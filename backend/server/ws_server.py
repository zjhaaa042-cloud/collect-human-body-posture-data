import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from datetime import datetime, timezone
import numpy as np
import websockets
from websockets.http11 import Response
from websockets.datastructures import Headers
from typing import Set, Callable, Any, Mapping, Optional
from loguru import logger

from ..core.camera_manager import CameraManager
from ..core.camera_adapters import (
    CameraAdapterRegistry,
    FrameBundle,
    OrbbecCameraAdapter,
    RealSenseCameraAdapter,
)
from ..core.depth_analyzer import DepthAnalyzer, DistanceStatus
from ..core.data_collector import DataCollector, CaptureConfig
from ..core.frame_contract import FrameContractError, validate_frame_contract
from ..core.protocol_store import (
    IncompleteSubjectError,
    ProtocolStore,
    ProtocolStoreError,
)
from ..application.dual_workflow import DualWorkflowService
from ..protocol import (
    Condition,
    full31_no_lux,
    full36,
    format_condition_id,
    gemini27,
    measurement_definitions,
    primary3,
    validate_subject_id,
)
from ..voice.recognizer import VoiceRecognizer
from ..voice.synthesizer import VoiceSynthesizer
from ..voice.command_parser import VoiceCommandParser, VoiceCommand
from ..utils.frame_processor import FrameProcessor
from ..config.settings import get_settings, save_settings

_ERROR_MESSAGES = {
    json.JSONDecodeError: "请求格式无效",
    KeyError: "缺少必要参数",
    ValueError: "参数值无效",
    PermissionError: "权限不足",
    FileNotFoundError: "资源未找到",
}

_MAX_LENGTHS = {
    "session_name": 100,
    "text": 500,
    "filename": 50,
}

_SAFE_PATTERN = re.compile(r'^[\w\-\u4e00-\u9fff\s]+$')
_FILENAME_PATTERN = re.compile(r'^[\w\-\.]+$')

_ALLOWED_AUTH_ORIGINS = {
    "http://localhost:3000",
    "http://127.0.0.1:3000",
}

_MAX_AUTO_STABLE_FRAMES = 120
_MAX_AUTO_DISTANCE_DELTA_MM = 1000.0
_MAX_AUTO_CAPTURE_COUNT = 100
_MAX_AUTO_CAPTURE_INTERVAL_SEC = 60.0
_LEGACY_WRITES_ENABLED = False


def _choose_native_output_directory() -> str:
    """Open the local Windows folder chooser for browser-based workspaces."""
    if os.name != "nt":
        raise RuntimeError("文件夹选择仅支持本机 Windows 采集服务")
    try:
        from tkinter import Tk, filedialog

        dialog_root = Tk()
        dialog_root.withdraw()
        dialog_root.attributes("-topmost", True)
        try:
            selected = filedialog.askdirectory(
                parent=dialog_root,
                title="选择双机采集数据输出文件夹",
                mustexist=False,
            )
        finally:
            dialog_root.destroy()
    except Exception as exc:
        raise RuntimeError(f"无法打开 Windows 文件夹选择窗口：{exc}") from exc
    return str(selected or "")

_PROTOCOL_VERSION = "RealAnthro-RGBD-v1.0"
_PROTOCOL_CAPTURE_POLICY_VERSION = "realanthro-capture-v1.1"
_DEFAULT_PROTOCOL_PROFILE = "full31_no_lux"
_PROTOCOL_PROFILES = {
    "primary3": primary3,
    "gemini27": gemini27,
    "full31_no_lux": full31_no_lux,
    "full36": full36,
}
_PROFILE_NAMES_ZH = {
    "primary3": "核心 3 条（联调）",
    "gemini27": "Gemini 27 条",
    "full31_no_lux": "Full-31（当前推荐，无照度计）",
    "full36": "Full-36（需照度计与受控灯光）",
}
_CAMERA_BACKEND_BY_CODE = {"C336L": "orbbec", "CD435I": "realsense"}
_OPERATOR_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_PROTOCOL_BURST_FRAMES = 5
_PROTOCOL_BURST_INTERVAL_SEC = 0.15
_PROTOCOL_QC_VERSION = "realanthro-qc-pilot-v1"
_REVIEW_EVIDENCE_TOKEN_TTL_SEC = 600.0
_REQUIRED_OPERATOR_CONFIRMATIONS = (
    "distance_marker",
    "pose_view_clothing",
    "full_body_visible",
)
_REQUIRED_QC_CHECK_COUNTS = {
    "BURST_FRAME_COUNT": 1,
    "REQUIRED_MODALITIES": 5,
    "IMAGE_FORMAT_AND_SHAPE": 5,
    "RGBD_FRAME_CONTRACT": 5,
    "CALIBRATION_COMPLETE": 5,
    "DEPTH_RAW_VALID_RATIO": 5,
    "DEPTH_ALIGNED_VALID_RATIO": 5,
    "STREAM_TIMESTAMPS_AND_SKEW": 5,
    "STREAM_FRAME_NUMBERS_PRESENT": 5,
    "CALIBRATION_STABLE_ACROSS_BURST": 1,
    "FRAME_NUMBERS_STRICTLY_INCREASING": 1,
    "BURST_DEVICE_INTERVAL_HARD": 1,
    "HUMAN_CONTENT_MANUAL_REVIEW": 1,
}

_VIEW_INSTRUCTIONS = {
    0: "V000 正面朝向相机；脚中心对准 BODY_CENTER。",
    90: "V090 按角度地垫从 V000 沿俯视顺时针方向转到 90°；左侧面对相机。",
    180: "V180 背面朝向相机；不要回头。",
    270: "V270 按角度地垫从 V000 沿俯视顺时针方向转到 270°；右侧面对相机。",
}
_POSE_INSTRUCTIONS = {
    "P1": (
        "Primary A-pose：双脚约肩宽、身体直立、目视前方；双臂离躯干 "
        "20–30°，肘自然伸直、手掌自然；不挺胸、不收腹、正常呼吸。"
    ),
    "P2": "Natural pose：双脚自然站立，双手自然下垂，不刻意展开手臂。",
    "P3": "Wide A-pose：在 P1 基础上将双臂展开约 40–45°。",
}
_CLOTHING_INSTRUCTIONS = {
    "CF": (
        "Controlled fitted：赤脚，统一哑光贴身上衣和贴身短裤/运动裤；"
        "无外套、裙子、宽松衣物、围巾或大包。"
    ),
    "CN": "Natural clothing：保留到场日常衣着，但必须脱鞋并移除大包、围巾等附件。",
}


class _WebSocketHandshakeNoiseFilter(logging.Filter):
    def filter(self, record):
        if record.getMessage() != "opening handshake failed" or not record.exc_info:
            return True
        exc = record.exc_info[1]
        if not isinstance(exc, websockets.exceptions.InvalidMessage):
            return True
        cause = exc.__cause__
        return not (
            isinstance(cause, EOFError)
            and "connection closed while reading HTTP request line" in str(cause)
        )


def _get_websocket_logger():
    ws_logger = logging.getLogger("body_posture.websockets")
    if not any(isinstance(item, _WebSocketHandshakeNoiseFilter) for item in ws_logger.filters):
        ws_logger.addFilter(_WebSocketHandshakeNoiseFilter())
    return ws_logger


def _is_local_address(remote_address) -> bool:
    if not remote_address:
        return False
    host = remote_address[0] if isinstance(remote_address, tuple) else remote_address
    host = str(host).strip().lower()
    if host.startswith("::ffff:"):
        host = host[7:]
    return host in {"localhost", "127.0.0.1", "::1"} or host.startswith("127.")


def _is_local_connection(connection) -> bool:
    try:
        return _is_local_address(connection.remote_address)
    except Exception:
        return False


def _is_allowed_auth_origin(origin: str) -> bool:
    return bool(origin and origin in _ALLOWED_AUTH_ORIGINS)


def _cors_headers_for_origin(origin: str):
    if origin and _is_allowed_auth_origin(origin):
        return [
            ("Access-Control-Allow-Origin", origin),
            ("Vary", "Origin"),
        ]
    return []


def _validate_field(value: str, field_name: str, pattern=None) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    value = value.strip()
    max_len = _MAX_LENGTHS.get(field_name, 200)
    if len(value) > max_len:
        raise ValueError(f"{field_name} exceeds max length {max_len}")
    if pattern and not pattern.match(value):
        raise ValueError(f"{field_name} contains invalid characters")
    return value


class WebSocketServer:
    def __init__(self, host: str = "localhost", port: int = 8765):
        self.host = host
        self.port = port
        self.clients: Set[websockets.WebSocketServerProtocol] = set()
        self.auth_token = secrets.token_urlsafe(32)

        self.settings = get_settings()
        self.camera = CameraManager(self.settings.camera.orientation)
        self.camera_registry = CameraAdapterRegistry(
            orbbec=OrbbecCameraAdapter(self.camera),
            realsense=RealSenseCameraAdapter(),
        )
        self.active_camera_adapter = self.camera_registry.adapters["orbbec"]
        self.depth_analyzer = DepthAnalyzer(
            target_distance_mm=self.settings.distance.target_distance_mm,
            tolerance_mm=self.settings.distance.tolerance_mm,
            roi_ratio=self.settings.distance.roi_ratio,
            min_distance_mm=self.settings.distance.min_distance_mm,
            max_distance_mm=self.settings.distance.max_distance_mm,
            min_edge_margin=self.settings.distance.min_edge_margin,
            min_body_depth_coverage=self.settings.distance.min_body_depth_coverage,
            min_quality_score=self.settings.distance.min_quality_score,
            pose_model_path=self.settings.distance.pose_model_path,
        )
        self.data_collector = DataCollector(self.settings.storage.output_dir)
        protocol_root = (
            Path(self.settings.storage.output_dir)
            / "realanthro_rgbd_v1"
            / "collections"
            / "default_collection"
        )
        self.protocol_store = ProtocolStore(
            protocol_root,
            dataset_phase="capture",
        )
        recovery_report = getattr(
            self.protocol_store, "startup_recovery_report", {}
        ) or {}
        if recovery_report.get("errors"):
            logger.error(
                f"Protocol storage startup recovery reported errors: "
                f"{recovery_report['errors']}"
            )
        elif recovery_report.get("subjects_changed"):
            logger.warning(
                "Protocol storage reconciled interrupted work at startup: "
                f"{recovery_report}"
            )
        self.active_protocol_subject_id = None
        preview_size = self._preview_size_for_orientation(self.camera.orientation)
        self.frame_processor = FrameProcessor(
            preview_size=preview_size,
            jpeg_quality=self.settings.gui.jpeg_quality
        )

        self.voice_recognizer = None
        self.voice_synthesizer = None
        self.voice_parser = VoiceCommandParser()
        self.voice_protocol_armed = False
        self.loop = None  # Store reference to main event loop

        self.is_previewing = False
        self.is_capturing = False
        self.is_shutting_down = False
        self.capture_lock = asyncio.Lock()
        self.camera_lock = asyncio.Lock()
        self.camera_operation_lock = asyncio.Lock()
        self.preview_task = None
        self._last_color_preview = ""
        self._last_depth_preview = ""
        self._preview_miss_count = 0
        self.auto_capture_enabled = False
        self.auto_capture_options = {}
        self.auto_required_frames = 10
        self.auto_max_distance_delta_mm = 30.0
        self.auto_target_count = 3
        self.auto_capture_interval_sec = 1.0
        self.auto_stable_distances = []
        self.auto_captured_count = 0
        self.auto_state = "idle"
        self.auto_message = "自动采集未开启"
        self.auto_task = None
        self.auto_last_voice_key = None
        self._last_voice_command = None
        self._last_voice_command_at = 0.0
        self.shutdown_when_idle = (
            os.environ.get("BODY_COLLECTOR_SHUTDOWN_WHEN_IDLE") == "1"
        )
        self.dual_workflow = DualWorkflowService(self._dual_adapters)
        self._had_authenticated_client = False
        self._idle_shutdown_task = None

        self._setup_voice()

    def _preview_size_for_orientation(self, orientation: str):
        raw_w = self.settings.camera.width
        raw_h = self.settings.camera.height
        if orientation in {"portrait_cw", "portrait_ccw"}:
            output_w, output_h = raw_h, raw_w
            target_h = self.settings.gui.preview_width
            return max(1, round(target_h * output_w / output_h)), target_h
        target_w = self.settings.gui.preview_width
        return target_w, max(1, round(target_w * raw_h / raw_w))

    def _setup_voice(self):
        if self.settings.voice.enabled:
            try:
                self.voice_recognizer = VoiceRecognizer(self.settings.voice.model_path)
                self.voice_synthesizer = VoiceSynthesizer(
                    voice=self.settings.voice.tts_voice,
                    rate=self.settings.voice.tts_rate,
                    volume=self.settings.voice.tts_volume
                )
                recognition_started = self.voice_recognizer.start_listening(
                    self._on_voice_command,
                    self._on_voice_activity
                )
                if not recognition_started:
                    self.voice_recognizer = None
                    logger.info(
                        "Voice commands are disabled until a complete Vosk model is installed"
                    )
                logger.info("Voice output initialized")
            except Exception as e:
                logger.error(f"Failed to setup voice: {e}")

    def _on_voice_activity(self, is_active: bool):
        """Broadcast voice activity status to all clients"""
        if self.loop and not self.loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._broadcast_voice_activity(is_active),
                    self.loop
                )
            except Exception:
                pass

    def _on_voice_command(self, text: str):
        if self.voice_synthesizer and self.voice_synthesizer.is_speaking:
            return
        command = self.voice_parser.execute_command(text)
        if command == VoiceCommand.UNKNOWN:
            return
        now = time.monotonic()
        if (
            command == self._last_voice_command
            and now - self._last_voice_command_at < 1.5
        ):
            return
        self._last_voice_command = command
        self._last_voice_command_at = now
        if command == VoiceCommand.START_CAPTURE:
            if not self.active_protocol_subject_id:
                logger.info("Ignored voice capture: legacy capture is disabled")
                return
            if not self.voice_protocol_armed:
                logger.info("Ignored protocol voice capture: voice control is not armed")
                return
            if self.loop and not self.loop.is_closed():
                try:
                    state = self._protocol_subject_state(self.active_protocol_subject_id)
                    condition_id = state.get("next_condition_id")
                    coroutine = self._capture_protocol_condition(
                        None,
                        {"condition_id": condition_id},
                    )
                    asyncio.run_coroutine_threadsafe(coroutine, self.loop)
                except Exception:
                    pass
        elif command == VoiceCommand.STOP_CAPTURE:
            logger.info(
                "Ignored voice stop: a RealAnthro condition transaction cannot be cancelled"
            )
        elif command == VoiceCommand.FINISH:
            if not self.active_protocol_subject_id:
                logger.info("Ignored voice finish: legacy sessions are read-only")
                return
            if not self.voice_protocol_armed:
                logger.info("Ignored protocol voice finish: voice control is not armed")
                return
            if self.loop and not self.loop.is_closed():
                try:
                    coroutine = self._complete_protocol_subject(None, {})
                    asyncio.run_coroutine_threadsafe(coroutine, self.loop)
                except Exception:
                    pass

    def _build_capture_config(self, options: dict = None) -> CaptureConfig:
        options = options or {}
        return CaptureConfig(
            save_rgb=bool(options.get("save_rgb", self.settings.storage.save_rgb)),
            save_depth=bool(options.get("save_depth", self.settings.storage.save_depth)),
            save_pointcloud=bool(options.get("save_pointcloud", self.settings.storage.save_pointcloud)),
            colored_pointcloud=bool(options.get("colored_pointcloud", self.settings.storage.colored_pointcloud)),
            pointcloud_binary=bool(options.get("pointcloud_binary", self.settings.storage.pointcloud_binary)),
            quality_check=bool(options.get("quality_check", self.settings.storage.quality_check)),
            min_depth_coverage=self.settings.storage.min_depth_coverage,
            min_color_brightness=self.settings.storage.min_color_brightness,
            max_color_brightness=self.settings.storage.max_color_brightness
        )

    @staticmethod
    def _distance_payload(info) -> dict:
        return {
            "distance_mm": info.distance_mm,
            "status": info.status.value,
            "message": info.message,
            "confidence": info.confidence,
            "full_body_visible": info.full_body_visible,
            "visibility_score": info.visibility_score,
            "capture_quality": info.to_capture_quality(),
        }

    @staticmethod
    def _jsonable(value: Any) -> Any:
        if is_dataclass(value):
            return {
                key: WebSocketServer._jsonable(item)
                for key, item in asdict(value).items()
            }
        if isinstance(value, Mapping):
            return {
                str(key): WebSocketServer._jsonable(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [WebSocketServer._jsonable(item) for item in value]
        if hasattr(value, "item"):
            try:
                return value.item()
            except Exception:
                pass
        return value

    @staticmethod
    def _condition_payload(condition) -> dict:
        yaw = int(condition.view_yaw_deg)
        view_instruction = _VIEW_INSTRUCTIONS.get(
            yaw,
            (
                f"V{yaw:03d}：以 V000 正面为起点，按角度地垫的俯视顺时针"
                f"方向转到 {yaw}°；禁止凭感觉调整。"
            ),
        )
        return {
            "condition_id": format_condition_id(condition),
            **asdict(condition),
            "view_instruction": view_instruction,
            "pose_instruction": _POSE_INSTRUCTIONS[condition.pose_id],
            "clothing_instruction": _CLOTHING_INSTRUCTIONS[condition.clothing_id],
            "distance_instruction": (
                f"脚中心对准距当前相机光心地面投影 {condition.distance_mm} mm 的"
                "BODY_CENTER 标线。"
            ),
            "reposition_instruction": (
                "受试者必须完全离开站位区，再重新进入并重新对齐脚位。"
                if condition.repeat_id > 1
                else "首次站位，无离场重入要求。"
            ),
        }

    def _profile_conditions(self, profile_id: str):
        try:
            builder = _PROTOCOL_PROFILES[profile_id]
        except KeyError as exc:
            raise ValueError(f"未知条件矩阵: {profile_id}") from exc
        return builder()

    @staticmethod
    def _condition_from_payload(payload: Mapping[str, Any]) -> Condition:
        """Rebuild a frozen Condition while ignoring display-only snapshot keys."""

        metadata = payload.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        return Condition(
            camera_code=str(payload["camera_code"]),
            distance_mm=int(
                payload.get("distance_mm", payload.get("distance_nominal_mm"))
            ),
            view_yaw_deg=int(payload["view_yaw_deg"]),
            light_id=str(payload.get("light_id", "LSTD")),
            pose_id=str(payload.get("pose_id", "P1")),
            clothing_id=str(payload.get("clothing_id", "CF")),
            repeat_id=int(payload.get("repeat_id", 1)),
            suite=str(payload.get("suite") or metadata.get("suite") or "snapshot"),
        )

    def _subject_conditions(self, raw_state: Mapping[str, Any]):
        """Use the immutable subject snapshot; registry is only for new subjects."""

        snapshot = raw_state.get("protocol_snapshot")
        snapshot_conditions = (
            snapshot.get("conditions") if isinstance(snapshot, Mapping) else None
        )
        if isinstance(snapshot_conditions, list) and snapshot_conditions:
            conditions = tuple(
                self._condition_from_payload(item)
                for item in snapshot_conditions
                if isinstance(item, Mapping)
            )
            expected_ids = list(raw_state.get("expected_condition_ids") or [])
            actual_ids = [format_condition_id(item) for item in conditions]
            if actual_ids != expected_ids:
                raise ProtocolStoreError("协议快照条件顺序/ID 与受试者状态不一致")
            return conditions
        return self._profile_conditions(str(raw_state["profile_id"]))

    def _frozen_subject_qc_policy(
        self, raw_state: Mapping[str, Any], condition_id: str
    ) -> dict:
        snapshot = raw_state.get("protocol_snapshot")
        capture_policy = (
            snapshot.get("capture_policy") if isinstance(snapshot, Mapping) else None
        )
        bodies = (
            capture_policy.get("qc_policy_by_condition")
            if isinstance(capture_policy, Mapping)
            else None
        )
        hashes = (
            capture_policy.get("qc_policy_sha256_by_condition")
            if isinstance(capture_policy, Mapping)
            else None
        )
        policy = bodies.get(condition_id) if isinstance(bodies, Mapping) else None
        expected_hash = hashes.get(condition_id) if isinstance(hashes, Mapping) else None
        if not isinstance(policy, Mapping) or not expected_hash:
            raise ProtocolStoreError("受试者协议快照缺少该条件的冻结 QC policy")
        normalized = json.loads(json.dumps(policy, ensure_ascii=False))
        if not secrets.compare_digest(
            self._canonical_json_sha256(normalized), str(expected_hash)
        ):
            raise ProtocolStoreError("受试者冻结 QC policy 哈希不一致")
        return normalized

    def _confirmation_nonce(self, subject_id: str, condition_id: str) -> str:
        nonces = getattr(self, "_protocol_confirmation_nonces", None)
        if nonces is None:
            nonces = {}
            self._protocol_confirmation_nonces = nonces
        key = (subject_id, condition_id)
        if key not in nonces:
            nonces[key] = secrets.token_urlsafe(18)
        return nonces[key]

    def _mark_protocol_reconciliation_required(self, subject_id: str) -> None:
        subjects = getattr(self, "_protocol_reconciliation_required_subjects", None)
        if subjects is None:
            subjects = set()
            self._protocol_reconciliation_required_subjects = subjects
        subjects.add(subject_id)

    def _protocol_reconciliation_required(self, subject_id: str) -> bool:
        return subject_id in getattr(
            self, "_protocol_reconciliation_required_subjects", set()
        )

    def _assert_protocol_subject_writable(
        self,
        subject_id: str,
        *,
        require_camera_fingerprint: bool = False,
    ) -> None:
        if self._protocol_reconciliation_required(subject_id):
            raise ProtocolStoreError(
                "该受试者存在已落盘但待恢复的账本事务；请停止操作并重启采集服务"
            )
        if require_camera_fingerprint:
            state = self.protocol_store.get_subject_state(subject_id)
            snapshot = state.get("protocol_snapshot")
            policy = (
                snapshot.get("capture_policy")
                if isinstance(snapshot, Mapping)
                else None
            )
            if not isinstance(policy, Mapping) or not bool(
                policy.get("lock_camera_fingerprint", False)
            ):
                raise ProtocolStoreError(
                    "该受试者使用旧版采集策略，缺少跨条件相机指纹门禁；"
                    "禁止继续新增图像，请新建 v1.1 协议受试者"
                )

    def _protocol_catalog(self) -> dict:
        profiles = []
        for profile_id, builder in _PROTOCOL_PROFILES.items():
            profiles.append({
                "profile_id": profile_id,
                "name": _PROFILE_NAMES_ZH[profile_id],
                "condition_count": len(builder()),
                "requires_lux": profile_id == "full36",
                "recommended": profile_id == _DEFAULT_PROTOCOL_PROFILE,
                "available": profile_id != "full36",
                "unavailable_reason": (
                    "当前未配置照度计和可复现受控灯光"
                    if profile_id == "full36"
                    else ""
                ),
            })
        measurements = []
        for definition in measurement_definitions():
            item = asdict(definition)
            item["field_names"] = list(item["field_names"])
            item["required_equipment"] = list(item["required_equipment"])
            measurements.append(item)
        recovery = getattr(self.protocol_store, "startup_recovery_report", {}) or {}
        camera_readiness = {}
        registry = getattr(self, "camera_registry", None)
        if registry is not None:
            for camera_code, backend in _CAMERA_BACKEND_BY_CODE.items():
                adapter = registry.adapters.get(backend)
                status = adapter.get_status() if adapter is not None else {}
                devices = adapter.list_devices() if adapter is not None else []
                matching = [
                    item for item in devices if item.get("camera_code") == camera_code
                ]
                camera_readiness[camera_code] = {
                    "backend": backend,
                    "sdk_available": bool(status.get("sdk_available", True)),
                    "device_detected": bool(matching),
                    "connected": bool(
                        status.get("connected")
                        and (status.get("device") or {}).get("camera_code") == camera_code
                    ),
                    "devices": matching,
                }
        return {
            "protocol_version": _PROTOCOL_VERSION,
            "profiles": profiles,
            "measurements": measurements,
            "default_profile_id": _DEFAULT_PROTOCOL_PROFILE,
            "burst_frame_count": _PROTOCOL_BURST_FRAMES,
            "anchor_frame": "F03",
            "not_in_capture_gate": ["SMPL-X", "dataset_release", "dataset_sealing"],
            "camera_readiness": camera_readiness,
            "storage_recovery": {
                "subjects_scanned": recovery.get("subjects_scanned", 0),
                "subjects_changed": recovery.get("subjects_changed", 0),
                "recovered_commits": recovery.get("recovered_commits", 0),
                "aborted_attempts": recovery.get("aborted_attempts", 0),
                "write_failed_attempts": recovery.get("write_failed_attempts", 0),
                "error_count": len(recovery.get("errors", [])),
                "errors": recovery.get("errors", []),
            },
        }

    def _raw_subject_states(self) -> list:
        list_method = getattr(self.protocol_store, "list_subjects", None)
        if callable(list_method):
            return list(list_method())

        # Compatibility with an older ProtocolStore during an in-place upgrade.
        roots = [
            getattr(self.protocol_store, "phase_dir", None),
            getattr(self.protocol_store, "base_dir", Path(".")) / "subjects",
        ]
        states = []
        seen = set()
        for root in roots:
            if not root or not Path(root).exists():
                continue
            for subject_dir in Path(root).iterdir():
                if not subject_dir.is_dir() or subject_dir.name in seen:
                    continue
                try:
                    state = self.protocol_store.get_subject_state(subject_dir.name)
                except Exception:
                    continue
                seen.add(subject_dir.name)
                states.append(state)
        return sorted(
            states,
            key=lambda item: (str(item.get("created_at", "")), item.get("subject_id", "")),
        )

    def _measurement_records_from_state(self, anthropometry: Mapping[str, Any]) -> list:
        direct_records = anthropometry.get("records")
        if isinstance(direct_records, list):
            return direct_records
        measurements = anthropometry.get("measurements")
        if not isinstance(measurements, Mapping):
            return []

        records = []
        for definition in measurement_definitions():
            for field_name in definition.field_names:
                source = measurements.get(field_name)
                if source is None and len(definition.field_names) == 1:
                    source = measurements.get(definition.measurement_id)
                if not isinstance(source, Mapping):
                    continue
                records.append({
                    "measurement_id": definition.measurement_id,
                    "field_name": field_name,
                    "m1": source.get("measurement_1"),
                    "m2": source.get("measurement_2"),
                    "m3": source.get("measurement_3"),
                    "final_value": source.get("final_value"),
                })
        return records

    def _protocol_subject_state(self, subject_id: str) -> dict:
        raw = self.protocol_store.get_subject_state(subject_id)
        profile_id = raw["profile_id"]
        conditions = []
        captured = 0
        for condition in self._subject_conditions(raw):
            payload = self._condition_payload(condition)
            condition_state = raw.get("conditions", {}).get(payload["condition_id"], {})
            attempt_ids = list(condition_state.get("attempt_ids", []))
            latest_attempt_id = attempt_ids[-1] if attempt_ids else None
            latest_attempt = raw.get("attempts", {}).get(latest_attempt_id, {})
            review_attempt_id = None
            if condition_state.get("status") == "REVIEW_REQUIRED":
                review_attempt_id = next(
                    (
                        attempt_id
                        for attempt_id in reversed(attempt_ids)
                        if raw.get("attempts", {}).get(attempt_id, {}).get("quality_status")
                        == "WARN"
                        and raw.get("attempts", {}).get(attempt_id, {}).get(
                            "review_status"
                        )
                        in {None, "PENDING"}
                    ),
                    None,
                )
            payload.update({
                "status": condition_state.get("status", "PENDING"),
                "attempt_ids": attempt_ids,
                "accepted_attempt_id": condition_state.get("accepted_attempt_id"),
                "latest_attempt_id": latest_attempt_id,
                "review_attempt_id": review_attempt_id,
                "qc": latest_attempt.get("qc")
                if isinstance(latest_attempt, Mapping)
                else None,
                "review": latest_attempt.get("review")
                if isinstance(latest_attempt, Mapping)
                else None,
                "confirmation_nonce": self._confirmation_nonce(
                    subject_id, payload["condition_id"]
                ),
            })
            if payload["status"] == "CAPTURED":
                captured += 1
            conditions.append(payload)

        expected = len(conditions)
        missing = expected - captured
        next_condition = next(
            (item for item in conditions if item["status"] != "CAPTURED"),
            None,
        )
        protocol_snapshot = raw.get("protocol_snapshot")
        frozen_measurements = (
            protocol_snapshot.get("measurements", [])
            if isinstance(protocol_snapshot, Mapping)
            else []
        )
        required_measurement_ids = [
            str(item.get("measurement_id") or "")
            for item in frozen_measurements
            if isinstance(item, Mapping) and bool(item.get("required"))
        ]
        if not required_measurement_ids:
            required_measurement_ids = [
                definition.measurement_id
                for definition in measurement_definitions()
                if definition.required
            ]
        anthro_raw = dict(raw.get("anthropometry", {}))
        anthro_complete = anthro_raw.get("status") == "COMPLETE"
        missing_required = [] if anthro_complete else required_measurement_ids
        blockers = []
        if missing:
            blockers.append(f"尚有 {missing} 个采集条件未通过")
        if not anthro_complete:
            blockers.append(
                f"{len(required_measurement_ids)} 项必填人工测量尚未完整通过校验"
            )
        completed = raw.get("status") == "COMPLETE"
        integrity_report = None
        if (not missing and anthro_complete) or completed:
            integrity_report = self.protocol_store.completion_report(subject_id)
            if integrity_report.get("integrity_errors"):
                blockers.extend(integrity_report["integrity_errors"])
            if integrity_report.get("status") == "CORRUPTED":
                completed = False
                blockers.insert(0, "已完成数据的完整性复核失败，状态为 CORRUPTED")
        percent = round(captured * 100.0 / expected, 1) if expected else 0.0
        operator_id = str(raw.get("subject_metadata", {}).get("operator_id", ""))
        daily_equipment_check = None
        get_equipment_check = getattr(self.protocol_store, "get_equipment_check", None)
        if operator_id and callable(get_equipment_check):
            try:
                daily_equipment_check = get_equipment_check(operator_id)
            except Exception as exc:
                logger.warning("Unable to load daily equipment check: {}", exc)
        return {
            "subject_id": raw["subject_id"],
            "reconciliation_required": self._protocol_reconciliation_required(
                raw["subject_id"]
            ),
            "status": (
                "CORRUPTED"
                if integrity_report and integrity_report.get("status") == "CORRUPTED"
                else raw.get("status", "ACTIVE")
            ),
            "profile_id": profile_id,
            "protocol_version": raw.get("protocol_version", _PROTOCOL_VERSION),
            "protocol_snapshot_sha256": raw.get("protocol_snapshot_sha256")
            or (raw.get("protocol_snapshot") or {}).get("sha256"),
            "subject_metadata": raw.get("subject_metadata", {}),
            "measurement_definitions": frozen_measurements,
            "created_at": raw.get("created_at"),
            "completed_at": raw.get("completed_at"),
            "conditions": conditions,
            "next_condition_id": next_condition["condition_id"] if next_condition else None,
            "next_camera_code": next_condition["camera_code"] if next_condition else None,
            "progress": {
                "expected": expected,
                "captured": captured,
                "missing": missing,
                "percent": percent,
            },
            "anthropometry": {
                **anthro_raw,
                "records": self._measurement_records_from_state(anthro_raw),
                "missing_required": missing_required,
                "complete": anthro_complete,
            },
            "daily_equipment_check": daily_equipment_check,
            "completion": {
                "can_complete": not blockers and not completed,
                "blockers": blockers,
                "completed": completed,
                "completed_at": raw.get("completed_at"),
                "status": integrity_report.get("status")
                if integrity_report
                else ("COMPLETE" if completed else "INCOMPLETE"),
                "integrity_errors": integrity_report.get("integrity_errors", [])
                if integrity_report
                else [],
            },
        }

    def _apply_protocol_distance_target(
        self,
        state: Mapping[str, Any],
        condition_id: Optional[str] = None,
    ) -> Optional[Mapping[str, Any]]:
        target_id = condition_id or state.get("next_condition_id")
        condition = next(
            (
                item
                for item in state.get("conditions", [])
                if item.get("condition_id") == target_id
            ),
            None,
        )
        if not condition:
            return None
        if not hasattr(self, "depth_analyzer"):
            return condition
        target = float(condition["distance_mm"])
        tolerance = max(150.0, target * 0.10)
        min_distance = max(300.0, target - tolerance)
        max_distance = target + tolerance
        configure_window = getattr(
            self.depth_analyzer,
            "configure_distance_window",
            None,
        )
        if callable(configure_window):
            configure_window(target, tolerance, min_distance, max_distance)
        elif (
            self.depth_analyzer.target != target
            or self.depth_analyzer.tolerance != tolerance
            or getattr(self.depth_analyzer, "min_distance", None) != min_distance
            or getattr(self.depth_analyzer, "max_distance", None) != max_distance
        ):
            self.depth_analyzer.target = target
            self.depth_analyzer.tolerance = tolerance
            self.depth_analyzer.min_distance = min_distance
            self.depth_analyzer.max_distance = max_distance
            self.depth_analyzer.reset()
        return condition

    async def _send_protocol_state(self, websocket, subject_id: str):
        state = self._protocol_subject_state(subject_id)
        if subject_id == self.active_protocol_subject_id:
            self._apply_protocol_distance_target(state)
        message = {"type": "protocol_subject_state", "data": state}
        await self._emit_protocol_message(websocket, message)
        return state

    async def _set_protocol_preview_condition(self, websocket, data: dict):
        subject_id = validate_subject_id(
            str(data.get("subject_id", "")).strip().upper()
        )
        condition_id = str(data.get("condition_id", "")).strip()
        if not condition_id:
            raise ValueError("预览条件不能为空")
        if subject_id != self.active_protocol_subject_id:
            raise ValueError("预览条件必须属于当前活动受试者")
        state = self._protocol_subject_state(subject_id)
        condition = self._apply_protocol_distance_target(state, condition_id)
        if condition is None:
            raise ValueError("预览条件不属于当前受试者的矩阵")
        result = {
            "subject_id": subject_id,
            "condition_id": condition_id,
            "distance_mm": int(condition["distance_mm"]),
        }
        await self._emit_protocol_message(
            websocket, {"type": "protocol_preview_condition", "data": result}
        )
        return result

    async def _emit_protocol_message(self, websocket, message: dict):
        """Keep subject state scoped to the requesting client.

        Voice-triggered commands have no requesting socket and are broadcast;
        browser commands are always answered only on their own connection.
        """

        if websocket is None:
            await self._broadcast(message)
        else:
            try:
                await asyncio.wait_for(
                    websocket.send(json.dumps(message, ensure_ascii=False)),
                    timeout=0.5,
                )
            except Exception as exc:
                logger.warning(f"Protocol client notification failed: {exc}")

    async def _send_protocol_subjects(self, websocket):
        subjects = []
        for raw in self._raw_subject_states():
            try:
                state = self._protocol_subject_state(raw["subject_id"])
            except Exception as exc:
                logger.error(
                    f"Subject {raw.get('subject_id')} is unreadable and remains visible: {exc}"
                )
                expected = int(raw.get("expected_conditions", 0) or 0)
                captured = int(raw.get("captured_conditions", 0) or 0)
                subjects.append({
                    "subject_id": raw.get("subject_id"),
                    "status": "UNREADABLE",
                    "profile_id": raw.get("profile_id"),
                    "created_at": raw.get("created_at"),
                    "completed_at": raw.get("completed_at"),
                    "progress": {
                        "expected": expected,
                        "captured": captured,
                        "missing": max(0, expected - captured),
                        "percent": round(captured * 100.0 / expected, 1)
                        if expected
                        else 0.0,
                    },
                    "error_code": "SUBJECT_STATE_UNREADABLE",
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue
            subjects.append({
                "subject_id": state["subject_id"],
                "status": state["status"],
                "profile_id": state["profile_id"],
                "created_at": state["created_at"],
                "completed_at": state["completed_at"],
                "progress": state["progress"],
            })
        await self._emit_protocol_message(websocket, {
            "type": "protocol_subject_list",
            "data": {"subjects": subjects},
        })

    def _protocol_capture_policy(self, conditions: tuple[Condition, ...]) -> dict:
        """Freeze every capture/QC rule needed to validate future attempts."""

        qc_policies = {
            format_condition_id(item): self._protocol_qc_policy(item)
            for item in conditions
        }
        return {
            "burst_frame_count": _PROTOCOL_BURST_FRAMES,
            "anchor_frame": "F03",
            "burst_interval_target_ms": int(_PROTOCOL_BURST_INTERVAL_SEC * 1000),
            "required_modalities": [
                "rgb",
                "depth_raw",
                "depth_aligned",
                "ir_left",
                "ir_right",
            ],
            "optional_modalities": [],
            "qc_policy_version": _PROTOCOL_QC_VERSION,
            "qc_policy_sha256_by_condition": {
                condition_id: self._canonical_json_sha256(policy)
                for condition_id, policy in qc_policies.items()
            },
            "qc_policy_by_condition": qc_policies,
            "required_qc_check_counts": dict(_REQUIRED_QC_CHECK_COUNTS),
            "warn_requires_manual_review": True,
            "strict_qc_contract": True,
            "require_anthropometry_equipment": False,
            "lock_camera_fingerprint": True,
            "view_angle_direction": "clockwise_from_overhead",
        }

    async def _create_protocol_subject(self, websocket, data: dict):
        subject_id = validate_subject_id(str(data.get("subject_id", "")).strip().upper())
        profile_id = str(data.get("profile_id") or _DEFAULT_PROTOCOL_PROFILE)
        conditions = self._profile_conditions(profile_id)
        metadata = dict(data.get("metadata") or {})
        if metadata.get("consent_internal") is not True:
            raise ValueError("必须确认受试者已同意本项目内部采集")
        operator_id = str(metadata.get("operator_id") or "").strip()
        if operator_id and not _OPERATOR_ID_PATTERN.fullmatch(operator_id):
            raise ValueError("操作员编号只能包含字母、数字、下划线或连字符，长度 1–32")
        if profile_id == "full36":
            raise ValueError("Full-36 当前被后端禁用；完成照度计、灯具和逐条件 lux 元数据支持后再启用")
        confirmed_at = datetime.now(timezone.utc).isoformat()
        metadata.update({
            "operator_id": operator_id,
            "collection_scope": "capture_only",
            "smplx_deferred": True,
            "consent_internal_version": "capture-consent-v1",
            "consent_internal_confirmed_at": confirmed_at,
            "created_by_app_at": confirmed_at,
        })
        created = self.protocol_store.create_subject(
            subject_id=subject_id,
            protocol_version=_PROTOCOL_VERSION,
            profile_id=profile_id,
            subject_metadata=metadata,
            expected_conditions=[self._condition_payload(item) for item in conditions],
            capture_policy_version=_PROTOCOL_CAPTURE_POLICY_VERSION,
            capture_policy=self._protocol_capture_policy(conditions),
        )
        self.active_protocol_subject_id = subject_id
        try:
            state = await self._send_protocol_state(websocket, subject_id)
            await self._send_protocol_subjects(websocket)
            await self._broadcast({
                "type": "protocol_subject_list_changed",
                "data": {"subject_id": subject_id, "action": "created"},
            })
            return state
        except Exception as exc:
            # The subject directory and immutable snapshot already exist.  Do
            # not make the UI believe creation rolled back or encourage reuse
            # of the same subject ID.
            logger.exception("Subject created but initial state delivery failed")
            fallback = {
                "success": True,
                "operation_success": True,
                "committed": True,
                "subject_id": subject_id,
                "created_state": created,
                "post_commit_error": f"{type(exc).__name__}: {exc}",
                "message": "受试者已创建，但界面初始化失败；请刷新受试者列表",
            }
            try:
                await self._emit_protocol_message(
                    websocket, {"type": "protocol_subject_created", "data": fallback}
                )
            except Exception:
                pass
            return fallback

    async def _select_protocol_subject(self, websocket, data: dict):
        subject_id = validate_subject_id(str(data.get("subject_id", "")).strip().upper())
        self.protocol_store.get_subject_state(subject_id)
        self.active_protocol_subject_id = subject_id
        return await self._send_protocol_state(websocket, subject_id)

    @staticmethod
    def _canonical_json_sha256(value: Mapping[str, Any]) -> str:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _protocol_qc_policy(self, condition: Condition) -> dict:
        """Build the immutable, condition-specific QC policy snapshot."""

        distance_tolerance = max(400.0, float(condition.distance_mm) * 0.20)
        return {
            "schema_version": "1.0",
            "policy_version": _PROTOCOL_QC_VERSION,
            "mode": "pilot_review",
            "required_frame_count": _PROTOCOL_BURST_FRAMES,
            "anchor_frame": "F03",
            "required_modalities": [
                "rgb",
                "depth_raw",
                "depth_aligned",
                "ir_left",
                "ir_right",
            ],
            "hard_thresholds": {
                "stream_timestamp_skew_max_ms": 50.0,
                "burst_interval_min_exclusive_ms": 0.0,
                "burst_interval_max_ms": 500.0,
                "depth_valid_ratio_min": 0.05,
                "depth_scale_min_exclusive": 0.0,
                "calibration_required": [
                    "color_intrinsics",
                    "depth_raw_intrinsics",
                    "depth_aligned_intrinsics",
                    "depth_raw_to_color_extrinsics",
                ],
            },
            "review_thresholds": {
                "brightness_min": float(self.settings.storage.min_color_brightness),
                "brightness_max": float(self.settings.storage.max_color_brightness),
                "laplacian_variance_min": 25.0,
                "depth_valid_ratio_preferred_min": float(
                    self.settings.storage.min_depth_coverage
                ),
                "expected_distance_mm": float(condition.distance_mm),
                "expected_distance_tolerance_mm": distance_tolerance,
                "expected_distance_pixel_ratio_preferred_min": 0.03,
                "scene_region_height_ratio_preferred_min": 0.55,
                "scene_region_top_max_ratio": 0.20,
                "scene_region_bottom_min_ratio": 0.80,
                "burst_interval_preferred_min_ms": 90.0,
                "burst_interval_preferred_max_ms": 250.0,
            },
            "limitations": [
                "No Pilot-frozen human segmentation or pose model is enabled.",
                "Depth-band scene metrics cannot prove that a person is present.",
            ],
        }

    def _protocol_qc(
        self,
        frames: list[FrameBundle],
        condition,
        adapter=None,
        policy_snapshot: Mapping[str, Any] | None = None,
    ) -> dict:
        """Run reproducible hard checks and Pilot-only review metrics.

        The current project has no frozen human segmentation / pose model.  A
        depth band is therefore *not* treated as a human mask: a wall can
        satisfy it.  Content and label correctness always enter the explicit
        operator-review queue, while modality, dtype, synchronization and
        calibration failures remain hard failures.
        """

        import cv2

        adapter = adapter or self.active_camera_adapter
        backend = str(getattr(adapter, "backend", ""))
        policy = (
            json.loads(json.dumps(policy_snapshot, ensure_ascii=False))
            if isinstance(policy_snapshot, Mapping)
            else self._protocol_qc_policy(condition)
        )
        if str(policy.get("policy_version") or "") != _PROTOCOL_QC_VERSION:
            raise ProtocolStoreError("冻结 QC policy 版本与当前执行器不兼容")
        if int(policy.get("required_frame_count", 0)) != _PROTOCOL_BURST_FRAMES:
            raise ProtocolStoreError("冻结 QC policy 的 burst 帧数不受当前执行器支持")
        if policy.get("anchor_frame") != "F03":
            raise ProtocolStoreError("冻结 QC policy 的 anchor frame 不受当前执行器支持")
        distance_tolerance = float(
            policy["review_thresholds"]["expected_distance_tolerance_mm"]
        )
        policy_hash = self._canonical_json_sha256(policy)

        checks = []

        def add_check(
            code: str,
            status: str,
            message: str,
            *,
            frame: str | None = None,
            value: Any = None,
            unit: str | None = None,
            threshold: Any = None,
            category: str = "hard",
        ) -> None:
            item = {
                "code": code,
                "status": status,
                "category": category,
                "blocking": status == "FAIL",
                "message": message,
            }
            if frame is not None:
                item["frame"] = frame
            if value is not None:
                item["value"] = self._jsonable(value)
            if unit is not None:
                item["unit"] = unit
            if threshold is not None:
                item["threshold"] = self._jsonable(threshold)
            checks.append(item)

        add_check(
            "BURST_FRAME_COUNT",
            "PASS" if len(frames) == _PROTOCOL_BURST_FRAMES else "FAIL",
            (
                f"burst 共 {len(frames)} 帧"
                if len(frames) == _PROTOCOL_BURST_FRAMES
                else f"burst 必须为 {_PROTOCOL_BURST_FRAMES} 帧，实际 {len(frames)} 帧"
            ),
            value=len(frames),
            unit="frames",
            threshold={"equals": _PROTOCOL_BURST_FRAMES},
        )

        first_specs: dict[str, dict[str, Any]] = {}
        calibration_hashes: list[str] = []
        ordered_frame_numbers: list[int] = []
        ordered_device_timestamps: list[float] = []
        stream_number_series: dict[str, list[int]] = {}
        frame_metrics = []

        def canonical_streams(values: Mapping[str, Any]) -> dict[str, Any]:
            aliases = {
                "infrared_left": "ir_left",
                "infrared_right": "ir_right",
            }
            return {aliases.get(str(key), str(key)): value for key, value in values.items()}

        def image_spec(array: Any) -> dict[str, Any] | None:
            if not isinstance(array, np.ndarray):
                return None
            return {"shape": list(array.shape), "dtype": str(array.dtype)}

        for index, frame in enumerate(frames, 1):
            label = f"F{index:02d}"
            metric: dict[str, Any] = {"frame": label}
            modalities = {
                "rgb": frame.color,
                "depth_raw": frame.depth_raw,
                "depth_aligned": frame.depth_aligned,
                "ir_left": frame.infrared.get("left"),
                "ir_right": frame.infrared.get("right"),
            }
            missing = [name for name, array in modalities.items() if array is None]
            add_check(
                "REQUIRED_MODALITIES",
                "FAIL" if missing else "PASS",
                (
                    f"{label} 缺少模态：{', '.join(missing)}"
                    if missing
                    else f"{label} 五类协议模态齐全"
                ),
                frame=label,
                value={"missing": missing, "backend": backend},
                threshold={"required": policy["required_modalities"]},
            )
            metric["missing_modalities"] = missing

            format_errors = []
            for name, array in modalities.items():
                if array is None:
                    continue
                spec = image_spec(array)
                metric[f"{name}_spec"] = spec
                if name == "rgb":
                    valid_format = (
                        array.dtype == np.uint8
                        and array.ndim == 3
                        and array.shape[2] == 3
                    )
                elif name in {"depth_raw", "depth_aligned"}:
                    valid_format = array.dtype == np.uint16 and array.ndim == 2
                else:
                    valid_format = array.dtype in {np.dtype(np.uint8), np.dtype(np.uint16)} and array.ndim == 2
                if not valid_format:
                    format_errors.append(f"{name}={spec}")
                if name not in first_specs:
                    first_specs[name] = spec
                elif first_specs[name] != spec:
                    format_errors.append(
                        f"{name} 与 F01 不一致：{spec} != {first_specs[name]}"
                    )
            if frame.color is not None and frame.depth_aligned is not None:
                if frame.color.shape[:2] != frame.depth_aligned.shape[:2]:
                    format_errors.append(
                        "depth_aligned 高宽必须与 RGB 完全一致："
                        f"{frame.depth_aligned.shape[:2]} != {frame.color.shape[:2]}"
                    )
            add_check(
                "IMAGE_FORMAT_AND_SHAPE",
                "FAIL" if format_errors else "PASS",
                (
                    f"{label} 图像格式/尺寸错误：{'；'.join(format_errors)}"
                    if format_errors
                    else f"{label} dtype、通道数和 burst 尺寸一致"
                ),
                frame=label,
                value={name: image_spec(value) for name, value in modalities.items()},
                threshold={
                    "rgb": "uint8 HxWx3",
                    "depth_raw": "uint16 HxW",
                    "depth_aligned": "uint16 RGB_HxRGB_W",
                    "ir": "uint8|uint16 HxW",
                    "burst_consistency": True,
                },
            )

            try:
                frame_contract = validate_frame_contract(
                    frame, str(getattr(condition, "camera_code", "UNKNOWN"))
                )
                contract_error = None
            except FrameContractError as exc:
                frame_contract = None
                contract_error = str(exc)
            add_check(
                "RGBD_FRAME_CONTRACT",
                "FAIL" if contract_error else "PASS",
                (
                    f"{label} RGB-D 颜色/空间/时间契约失败：{contract_error}"
                    if contract_error
                    else f"{label} RGB 色序正确，aligned depth 与 RGB 同像素坐标，raw depth 标定可追溯"
                ),
                frame=label,
                value=frame_contract or {"error": contract_error},
                threshold={
                    "rgb_color_order": "RGB",
                    "aligned_depth_matches_rgb_pixels": True,
                    "raw_aligned_same_depth_frame": True,
                    "maximum_stream_timestamp_skew_ms": 75.0,
                },
            )
            metric["frame_contract"] = frame_contract or {"error": contract_error}

            intrinsics = self._jsonable(frame.intrinsics)
            extrinsics = self._jsonable(frame.extrinsics)
            calibration_errors = []
            expected_shapes = {
                "color": getattr(frame.color, "shape", (0, 0))[:2],
                "depth_raw": getattr(frame.depth_raw, "shape", (0, 0))[:2],
                "depth_aligned": getattr(frame.depth_aligned, "shape", (0, 0))[:2],
            }
            for stream_name in ("color", "depth_raw", "depth_aligned"):
                item = intrinsics.get(stream_name) if isinstance(intrinsics, Mapping) else None
                if not isinstance(item, Mapping):
                    calibration_errors.append(f"缺少 {stream_name} intrinsics")
                    continue
                if float(item.get("fx") or 0) <= 0 or float(item.get("fy") or 0) <= 0:
                    calibration_errors.append(f"{stream_name} fx/fy 无效")
                expected_height, expected_width = expected_shapes[stream_name]
                if (
                    int(item.get("width") or 0) != int(expected_width)
                    or int(item.get("height") or 0) != int(expected_height)
                ):
                    calibration_errors.append(
                        f"{stream_name} intrinsics 尺寸与图像不一致"
                    )
            depth_to_color = (
                extrinsics.get("depth_raw_to_color")
                if isinstance(extrinsics, Mapping)
                else None
            )
            if not isinstance(depth_to_color, Mapping):
                calibration_errors.append("缺少 depth_raw_to_color extrinsics")
            else:
                if len(depth_to_color.get("rotation") or []) != 9:
                    calibration_errors.append("depth_raw_to_color rotation 必须有 9 项")
                if len(depth_to_color.get("translation") or []) != 3:
                    calibration_errors.append("depth_raw_to_color translation 必须有 3 项")
            if not np.isfinite(float(frame.depth_scale)) or float(frame.depth_scale) <= 0:
                calibration_errors.append("depth_scale 必须为有限正数")
            calibration_payload = {
                "intrinsics": intrinsics,
                "extrinsics": extrinsics,
                "depth_scale_mm_per_unit": frame.depth_scale,
            }
            calibration_hash = hashlib.sha256(
                json.dumps(
                    calibration_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            calibration_hashes.append(calibration_hash)
            metric["calibration_sha256"] = calibration_hash
            add_check(
                "CALIBRATION_COMPLETE",
                "FAIL" if calibration_errors else "PASS",
                (
                    f"{label} 标定缺失/不一致：{'；'.join(calibration_errors)}"
                    if calibration_errors
                    else f"{label} 内参、外参和 depth scale 可复验"
                ),
                frame=label,
                value={"sha256": calibration_hash, "errors": calibration_errors},
                threshold={"required": policy["hard_thresholds"]["calibration_required"]},
            )

            if frame.color is not None and frame.color.ndim == 3:
                gray = frame.color.mean(axis=2).astype(np.uint8)
                brightness = float(gray.mean())
                blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
                metric.update(
                    {
                        "brightness_mean": brightness,
                        "laplacian_variance": blur_score,
                    }
                )
                brightness_min = policy["review_thresholds"]["brightness_min"]
                brightness_max = policy["review_thresholds"]["brightness_max"]
                brightness_ok = brightness_min <= brightness <= brightness_max
                add_check(
                    "RGB_BRIGHTNESS_PILOT",
                    "PASS" if brightness_ok else "WARN",
                    (
                        f"{label} RGB 亮度均值 {brightness:.1f}"
                        if brightness_ok
                        else f"{label} RGB 亮度均值 {brightness:.1f} 超出 Pilot 建议范围"
                    ),
                    frame=label,
                    value=brightness,
                    unit="gray_level",
                    threshold={"min": brightness_min, "max": brightness_max},
                    category="pilot_metric",
                )
                blur_min = policy["review_thresholds"]["laplacian_variance_min"]
                add_check(
                    "RGB_BLUR_PILOT",
                    "PASS" if blur_score >= blur_min else "WARN",
                    (
                        f"{label} Laplacian {blur_score:.1f}"
                        if blur_score >= blur_min
                        else f"{label} 可能模糊，Laplacian {blur_score:.1f}"
                    ),
                    frame=label,
                    value=blur_score,
                    threshold={"min": blur_min},
                    category="pilot_metric",
                )

            if frame.depth_raw is not None and frame.depth_raw.ndim == 2:
                raw_valid = (frame.depth_raw > 0) & (frame.depth_raw < 65535)
                raw_coverage = float(raw_valid.mean())
                metric["depth_raw_valid_ratio"] = raw_coverage
                hard_min = policy["hard_thresholds"]["depth_valid_ratio_min"]
                preferred_min = policy["review_thresholds"][
                    "depth_valid_ratio_preferred_min"
                ]
                if raw_coverage < hard_min:
                    coverage_status = "FAIL"
                elif raw_coverage < preferred_min:
                    coverage_status = "WARN"
                else:
                    coverage_status = "PASS"
                add_check(
                    "DEPTH_RAW_VALID_RATIO",
                    coverage_status,
                    f"{label} raw depth 有效率 {raw_coverage:.1%}",
                    frame=label,
                    value=raw_coverage,
                    unit="ratio",
                    threshold={"hard_min": hard_min, "preferred_min": preferred_min},
                    category="hard_and_pilot",
                )

            if frame.depth_aligned is not None and frame.depth_aligned.ndim == 2:
                aligned_valid = (frame.depth_aligned > 0) & (frame.depth_aligned < 65535)
                aligned_coverage = float(aligned_valid.mean())
                metric["depth_aligned_valid_ratio"] = aligned_coverage
                hard_min = policy["hard_thresholds"]["depth_valid_ratio_min"]
                preferred_min = policy["review_thresholds"][
                    "depth_valid_ratio_preferred_min"
                ]
                if aligned_coverage < hard_min:
                    aligned_status = "FAIL"
                elif aligned_coverage < preferred_min:
                    aligned_status = "WARN"
                else:
                    aligned_status = "PASS"
                add_check(
                    "DEPTH_ALIGNED_VALID_RATIO",
                    aligned_status,
                    f"{label} aligned depth 有效率 {aligned_coverage:.1%}",
                    frame=label,
                    value=aligned_coverage,
                    unit="ratio",
                    threshold={"hard_min": hard_min, "preferred_min": preferred_min},
                    category="hard_and_pilot",
                )

                depth_mm = frame.depth_aligned.astype(np.float32) * float(frame.depth_scale)
                near_expected = aligned_valid & (
                    depth_mm >= float(condition.distance_mm) - distance_tolerance
                ) & (
                    depth_mm <= float(condition.distance_mm) + distance_tolerance
                )
                expected_ratio = float(near_expected.mean())
                metric["expected_distance_pixel_ratio"] = expected_ratio
                preferred_ratio = policy["review_thresholds"][
                    "expected_distance_pixel_ratio_preferred_min"
                ]
                add_check(
                    "EXPECTED_DISTANCE_REGION_PILOT",
                    "PASS" if expected_ratio >= preferred_ratio else "WARN",
                    (
                        f"{label} 标称距离窗像素占比 {expected_ratio:.1%}；"
                        "该值仅为场景指标，不代表人体 mask"
                    ),
                    frame=label,
                    value=expected_ratio,
                    unit="ratio",
                    threshold={
                        "preferred_min": preferred_ratio,
                        "distance_mm": condition.distance_mm,
                        "tolerance_mm": distance_tolerance,
                    },
                    category="pilot_metric",
                )
                if near_expected.any():
                    mask = cv2.morphologyEx(
                        near_expected.astype(np.uint8),
                        cv2.MORPH_CLOSE,
                        np.ones((5, 5), dtype=np.uint8),
                    )
                    contours, _ = cv2.findContours(
                        mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                    )
                    if contours:
                        x, y, width, height = cv2.boundingRect(
                            max(contours, key=cv2.contourArea)
                        )
                        image_height = mask.shape[0]
                        height_ratio = height / image_height
                        top_ratio = y / image_height
                        bottom_ratio = (y + height) / image_height
                        metric["expected_depth_region_bbox"] = [x, y, width, height]
                        metric["expected_depth_region_height_ratio"] = height_ratio
                        region_ok = (
                            height_ratio
                            >= policy["review_thresholds"][
                                "scene_region_height_ratio_preferred_min"
                            ]
                            and top_ratio
                            <= policy["review_thresholds"]["scene_region_top_max_ratio"]
                            and bottom_ratio
                            >= policy["review_thresholds"]["scene_region_bottom_min_ratio"]
                        )
                        add_check(
                            "EXPECTED_DEPTH_REGION_GEOMETRY_PILOT",
                            "PASS" if region_ok else "WARN",
                            (
                                f"{label} 距离窗最大场景区域 bbox={[x, y, width, height]}；"
                                "不能据此判定人体或头脚"
                            ),
                            frame=label,
                            value={
                                "bbox": [x, y, width, height],
                                "height_ratio": height_ratio,
                                "top_ratio": top_ratio,
                                "bottom_ratio": bottom_ratio,
                            },
                            threshold={
                                "height_ratio_min": policy["review_thresholds"][
                                    "scene_region_height_ratio_preferred_min"
                                ],
                                "top_ratio_max": policy["review_thresholds"][
                                    "scene_region_top_max_ratio"
                                ],
                                "bottom_ratio_min": policy["review_thresholds"][
                                    "scene_region_bottom_min_ratio"
                                ],
                            },
                            category="pilot_metric",
                        )

            timestamps = canonical_streams(frame.stream_timestamps)
            stream_numbers = canonical_streams(frame.stream_frame_numbers)
            required_clock_streams = {
                "color",
                "depth_raw",
                "depth_aligned",
                "ir_left",
                "ir_right",
            }
            missing_timestamps = sorted(required_clock_streams - set(timestamps))
            if missing_timestamps:
                clock_status = "FAIL"
                skew_ms = None
            else:
                skew_ms = float(max(timestamps.values()) - min(timestamps.values()))
                clock_status = (
                    "PASS"
                    if skew_ms
                    <= policy["hard_thresholds"]["stream_timestamp_skew_max_ms"]
                    else "FAIL"
                )
            metric["stream_timestamp_skew_ms"] = skew_ms
            add_check(
                "STREAM_TIMESTAMPS_AND_SKEW",
                clock_status,
                (
                    f"{label} 缺少流时间戳：{', '.join(missing_timestamps)}"
                    if missing_timestamps
                    else f"{label} 流间时间差 {skew_ms:.3f} ms"
                ),
                frame=label,
                value={"timestamps": timestamps, "skew_ms": skew_ms},
                threshold={
                    "required_streams": sorted(required_clock_streams),
                    "skew_max_ms": policy["hard_thresholds"][
                        "stream_timestamp_skew_max_ms"
                    ],
                },
            )

            missing_stream_numbers = sorted(required_clock_streams - set(stream_numbers))
            add_check(
                "STREAM_FRAME_NUMBERS_PRESENT",
                "FAIL" if missing_stream_numbers else "PASS",
                (
                    f"{label} 缺少流帧号：{', '.join(missing_stream_numbers)}"
                    if missing_stream_numbers
                    else f"{label} 各协议流帧号齐全"
                ),
                frame=label,
                value=stream_numbers,
                threshold={"required_streams": sorted(required_clock_streams)},
            )
            for name, number in stream_numbers.items():
                stream_number_series.setdefault(name, []).append(int(number))

            if frame.frame_number is None:
                add_check(
                    "PRIMARY_FRAME_NUMBER_PRESENT",
                    "FAIL",
                    f"{label} 缺少主帧号",
                    frame=label,
                )
            else:
                ordered_frame_numbers.append(int(frame.frame_number))
            if frame.device_timestamp is None:
                add_check(
                    "PRIMARY_DEVICE_TIMESTAMP_PRESENT",
                    "FAIL",
                    f"{label} 缺少设备主时间戳",
                    frame=label,
                )
            else:
                ordered_device_timestamps.append(float(frame.device_timestamp))
            frame_metrics.append(metric)

        calibration_consistent = (
            len(calibration_hashes) == len(frames)
            and len(set(calibration_hashes)) == 1
        )
        add_check(
            "CALIBRATION_STABLE_ACROSS_BURST",
            "PASS" if calibration_consistent else "FAIL",
            (
                "五帧标定快照一致"
                if calibration_consistent
                else "五帧标定快照缺失或发生变化"
            ),
            value=calibration_hashes,
            threshold={"unique_hash_count": 1},
        )

        primary_numbers_increasing = (
            len(ordered_frame_numbers) == len(frames)
            and all(
                current > previous
                for previous, current in zip(
                    ordered_frame_numbers, ordered_frame_numbers[1:]
                )
            )
        )
        stream_increase_errors = {
            name: values
            for name, values in stream_number_series.items()
            if len(values) != len(frames)
            or any(
                current <= previous
                for previous, current in zip(values, values[1:])
            )
        }
        add_check(
            "FRAME_NUMBERS_STRICTLY_INCREASING",
            "PASS" if primary_numbers_increasing and not stream_increase_errors else "FAIL",
            (
                "主帧号及各流帧号严格递增"
                if primary_numbers_increasing and not stream_increase_errors
                else "存在缺失、重复或倒退的主帧号/流帧号"
            ),
            value={
                "primary": ordered_frame_numbers,
                "invalid_streams": stream_increase_errors,
            },
            threshold={"strictly_increasing": True, "count": len(frames)},
        )

        burst_intervals = []
        if len(ordered_device_timestamps) == len(frames):
            burst_intervals = [
                current - previous
                for previous, current in zip(
                    ordered_device_timestamps, ordered_device_timestamps[1:]
                )
            ]
        interval_hard_ok = (
            len(burst_intervals) == max(0, len(frames) - 1)
            and all(
                policy["hard_thresholds"]["burst_interval_min_exclusive_ms"]
                < interval
                <= policy["hard_thresholds"]["burst_interval_max_ms"]
                for interval in burst_intervals
            )
        )
        add_check(
            "BURST_DEVICE_INTERVAL_HARD",
            "PASS" if interval_hard_ok else "FAIL",
            (
                f"burst 设备时间间隔 {burst_intervals} ms"
                if interval_hard_ok
                else f"burst 设备时间间隔缺失或超出硬范围：{burst_intervals}"
            ),
            value=burst_intervals,
            unit="ms",
            threshold={
                "min_exclusive": policy["hard_thresholds"][
                    "burst_interval_min_exclusive_ms"
                ],
                "max": policy["hard_thresholds"]["burst_interval_max_ms"],
            },
        )
        preferred_interval_ok = bool(burst_intervals) and all(
            policy["review_thresholds"]["burst_interval_preferred_min_ms"]
            <= interval
            <= policy["review_thresholds"]["burst_interval_preferred_max_ms"]
            for interval in burst_intervals
        )
        add_check(
            "BURST_DEVICE_INTERVAL_PILOT",
            "PASS" if preferred_interval_ok else "WARN",
            "burst 间隔位于 Pilot 建议范围"
            if preferred_interval_ok
            else "burst 间隔偏离 150 ms 目标，请人工复核",
            value=burst_intervals,
            unit="ms",
            threshold={
                "preferred_min": policy["review_thresholds"][
                    "burst_interval_preferred_min_ms"
                ],
                "preferred_max": policy["review_thresholds"][
                    "burst_interval_preferred_max_ms"
                ],
                "target": int(_PROTOCOL_BURST_INTERVAL_SEC * 1000),
            },
            category="pilot_metric",
        )

        add_check(
            "HUMAN_CONTENT_MANUAL_REVIEW",
            "WARN",
            (
                "当前没有经 Pilot 冻结的人体分割/关键点模型；请人工复核 F03 中确有"
                "受试者、全身入框，且视角/姿态/服装标签正确。"
            ),
            value={"anchor_frame": "F03", "automated_human_mask": False},
            threshold={"operator_review_required": True},
            category="manual_review",
        )

        failures = [item for item in checks if item["status"] == "FAIL"]
        warning_checks = [item for item in checks if item["status"] == "WARN"]
        status = "FAIL" if failures else ("WARN" if warning_checks else "PASS")
        return {
            "schema_version": "1.0",
            "status": status,
            "policy_version": _PROTOCOL_QC_VERSION,
            "policy_sha256": policy_hash,
            "policy_snapshot": policy,
            "checks": checks,
            "reason_codes": [item["code"] for item in (*failures, *warning_checks)],
            "failure_codes": [item["code"] for item in failures],
            "warning_codes": [item["code"] for item in warning_checks],
            "hard_errors": [item["message"] for item in failures],
            "warnings": [item["message"] for item in warning_checks],
            "manual_review_required": bool(warning_checks) and not failures,
            "frame_metrics": frame_metrics,
            "burst_device_intervals_ms": burst_intervals,
            "calibration_sha256": calibration_hashes[0]
            if calibration_consistent and calibration_hashes
            else None,
            "condition_id": format_condition_id(condition),
        }

    def _capture_camera_metadata(
        self,
        frames: list[FrameBundle],
        operator_id: str,
        camera_code: str,
        operator_confirmations: Mapping[str, Any],
        qc: Mapping[str, Any],
    ) -> dict:
        first = frames[0]
        observed_streams = {
            "color": {
                "shape": list(first.color.shape),
                "dtype": str(first.color.dtype),
            },
            "depth_raw": {
                "shape": list(first.depth_raw.shape),
                "dtype": str(first.depth_raw.dtype),
            },
            "depth_aligned": {
                "shape": list(first.depth_aligned.shape),
                "dtype": str(first.depth_aligned.dtype),
            },
            **{
                f"ir_{name}": {"shape": list(value.shape), "dtype": str(value.dtype)}
                for name, value in first.infrared.items()
            },
        }
        source_metadata = self._jsonable(first.camera_metadata)
        stream_profiles = dict(source_metadata.get("stream_profiles") or {})
        for name, spec in observed_streams.items():
            height, width = spec["shape"][:2]
            stream_profiles.setdefault(
                name,
                {
                    "width": width,
                    "height": height,
                    "dtype": spec["dtype"],
                    "source": "observed_array",
                },
            )
        return {
            **source_metadata,
            "rgb_color_order": "RGB",
            "intrinsics": self._jsonable(first.intrinsics),
            "extrinsics": self._jsonable(first.extrinsics),
            "calibration_sha256": qc.get("calibration_sha256"),
            "depth_scale_mm_per_unit": first.depth_scale,
            "protocol_camera_code": camera_code,
            "operator_id": operator_id,
            "operator_confirmations": dict(operator_confirmations),
            "observed_streams": observed_streams,
            "stream_profiles": stream_profiles,
            "burst_frame_count": len(frames),
            "burst_interval_target_ms": int(_PROTOCOL_BURST_INTERVAL_SEC * 1000),
            "qc_policy_version": qc.get("policy_version"),
            "qc_policy_sha256": qc.get("policy_sha256"),
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _store_frames(frames: list[FrameBundle]) -> list[dict]:
        return [{
            "color": frame.color,
            "depth_raw": frame.depth_raw,
            "depth_aligned": frame.depth_aligned,
            "infrared": frame.infrared,
            "depth_scale": frame.depth_scale,
            "device_timestamp": frame.device_timestamp,
            "frame_number": frame.frame_number,
            "host_timestamp_ns": frame.host_timestamp_ns,
            "stream_timestamps": frame.stream_timestamps,
            "stream_frame_numbers": frame.stream_frame_numbers,
            "frame_camera_metadata": frame.camera_metadata,
        } for frame in frames]

    def _assert_protocol_camera(self, condition, adapter) -> Mapping[str, Any]:
        expected_backend = _CAMERA_BACKEND_BY_CODE[condition.camera_code]
        active_status = adapter.get_status()
        if adapter.backend != expected_backend or not active_status.get("connected"):
            camera_name = "Gemini 336L" if expected_backend == "orbbec" else "Intel D435i"
            raise ValueError(f"当前条件必须先连接 {camera_name}")
        active_device = active_status.get("device") or {}
        actual_camera_code = str(active_device.get("camera_code") or "")
        if actual_camera_code != condition.camera_code:
            raise ValueError(
                f"设备型号未通过协议识别：期望 {condition.camera_code}，实际 "
                f"{actual_camera_code or active_device.get('name') or '未知'}"
            )
        if not str(active_device.get("serial_number") or active_device.get("uid") or ""):
            raise ValueError("设备缺少可追溯序列号/UID，禁止协议采集")
        if condition.camera_code == "C336L" and set(
            getattr(self.camera, "enabled_ir_streams", []) or []
        ) != {"left", "right"}:
            raise ValueError("Gemini 336L 必须成功启用左右 IR 后才能采集")
        return active_status

    async def _acquire_protocol_burst(self, adapter) -> list[FrameBundle]:
        """Sample native synchronized frames at roughly the 150 ms target.

        RGB-D SDK queues often retain 30 fps frames while the coroutine sleeps.
        Simply sleeping then reading once can therefore return adjacent 33 ms
        frames.  We drain stale frames and only accept a bundle after at least
        90 ms on the device clock (falling back to the host monotonic clock).
        """

        import time

        frames: list[FrameBundle] = []
        accepted_host_ms: float | None = None
        last_device_ms: float | None = None
        per_frame_deadline_sec = 2.5
        while len(frames) < _PROTOCOL_BURST_FRAMES:
            deadline = time.monotonic() + per_frame_deadline_sec
            while True:
                bundle = await asyncio.to_thread(adapter.get_frames, 1500)
                if bundle is None:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"第 {len(frames) + 1} 帧获取失败")
                    continue
                now_host_ms = time.monotonic() * 1000.0
                device_ms = (
                    float(bundle.device_timestamp)
                    if bundle.device_timestamp is not None
                    else None
                )
                if not frames:
                    break
                device_delta = (
                    device_ms - last_device_ms
                    if device_ms is not None and last_device_ms is not None
                    else None
                )
                host_delta = now_host_ms - float(accepted_host_ms)
                if (
                    (device_delta is not None and device_delta >= 90.0)
                    or (device_delta is None and host_delta >= 90.0)
                ):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"第 {len(frames) + 1} 帧未达到独立采样间隔；"
                        f"device_delta={device_delta}, host_delta={host_delta:.1f} ms"
                    )
            frames.append(bundle)
            accepted_host_ms = now_host_ms
            last_device_ms = device_ms
        return frames

    async def _capture_protocol_condition(self, websocket, data: dict):
        if getattr(self, "is_shutting_down", False):
            raise ValueError("系统正在安全关闭，拒绝开始新的采集")
        if websocket is not None and not data.get("subject_id"):
            raise ValueError("协议写命令必须显式携带 subject_id")
        subject_id = str(data.get("subject_id") or self.active_protocol_subject_id or "")
        if not subject_id:
            raise ValueError("请先创建或选择受试者")
        self._assert_protocol_subject_writable(
            subject_id,
            require_camera_fingerprint=True,
        )
        state = self.protocol_store.get_subject_state(subject_id)
        if state.get("status") == "COMPLETE":
            raise ValueError("该受试者已完成，不能继续追加采集")
        condition_id = str(data.get("condition_id") or "")
        condition = next(
            (
                item
                for item in self._subject_conditions(state)
                if format_condition_id(item) == condition_id
            ),
            None,
        )
        if condition is None:
            raise ValueError("条件不属于当前受试者的矩阵")
        confirmations = data.get("confirmations")
        if not isinstance(confirmations, Mapping) or any(
            confirmations.get(key) is not True
            for key in _REQUIRED_OPERATOR_CONFIRMATIONS
        ):
            raise ValueError("开始采集前必须逐项确认距离、朝向/姿态/服装和全身入框")
        if condition.repeat_id > 1 and confirmations.get("repositioned") is not True:
            raise ValueError("R02/R03 必须确认受试者已完全离开站位区后重新进入")
        public_state = self._protocol_subject_state(subject_id)
        requested_state = next(
            item for item in public_state["conditions"] if item["condition_id"] == condition_id
        )
        if (
            condition_id != public_state.get("next_condition_id")
            and requested_state.get("status") != "CAPTURED"
        ):
            raise ValueError(
                f"请按协议顺序采集，下一条件是 {public_state.get('next_condition_id')}"
            )
        expected_nonce = requested_state.get("confirmation_nonce")
        supplied_nonce = str(confirmations.get("nonce") or "")
        if not expected_nonce or not secrets.compare_digest(supplied_nonce, expected_nonce):
            raise ValueError("本次现场确认已失效，请重新勾选后再采集")

        is_retake = requested_state.get("status") == "CAPTURED"
        retake_reason = data.get("retake_reason")
        target_attempt_id = data.get("target_attempt_id")
        invalidate_prior = data.get("invalidate_prior")
        if is_retake and (
            not str(retake_reason or "").strip()
            or target_attempt_id != requested_state.get("accepted_attempt_id")
            or not isinstance(invalidate_prior, bool)
        ):
            raise ValueError(
                "补采已通过条件时必须填写原因、指定当前 accepted attempt，并明确是否作废旧数据"
            )
        if self.capture_lock.locked():
            raise ValueError("正在采集中，请稍候")

        async with self.capture_lock:
            self.is_capturing = True
            attempt_id = None
            committed = False
            committed_attempt = None
            frames: list[FrameBundle] = []
            qc = None
            condition_payload = self._condition_payload(condition)
            try:
                # Re-check state, nonce and camera identity after acquiring the
                # workflow lock.  Camera connect/disconnect also re-check this
                # lock while holding camera_lock, closing the TOCTOU window.
                current_public_state = self._protocol_subject_state(subject_id)
                current_condition = next(
                    item
                    for item in current_public_state["conditions"]
                    if item["condition_id"] == condition_id
                )
                current_nonce = self._confirmation_nonce(subject_id, condition_id)
                if not secrets.compare_digest(supplied_nonce, current_nonce):
                    raise ValueError("本次现场确认已被使用，请刷新状态并重新确认")
                adapter = self.active_camera_adapter
                async with self.camera_lock:
                    if adapter is not self.active_camera_adapter:
                        raise ValueError("采集前摄像头发生切换，请重新确认条件")
                    self._assert_protocol_camera(condition, adapter)
                attempt_id = await asyncio.to_thread(
                    self.protocol_store.begin_capture_attempt,
                    subject_id,
                    condition_payload,
                    retake_reason,
                    target_attempt_id,
                    invalidate_prior,
                )
                condition_payload["attempt_id"] = attempt_id
                getattr(self, "_protocol_confirmation_nonces", {}).pop(
                    (subject_id, condition_id), None
                )
                if self.voice_synthesizer:
                    self.voice_synthesizer.speak("请保持姿势不动，两秒后采集。", blocking=False)
                await asyncio.sleep(2.0)
                async with self.camera_lock:
                    if adapter is not self.active_camera_adapter:
                        raise RuntimeError("burst 前摄像头已改变")
                    self._assert_protocol_camera(condition, adapter)
                    frames = await self._acquire_protocol_burst(adapter)

                qc = self._protocol_qc(
                    frames,
                    condition,
                    adapter=adapter,
                    policy_snapshot=self._frozen_subject_qc_policy(
                        state, condition_id
                    ),
                )
                committed_attempt = await asyncio.to_thread(
                    self.protocol_store.commit_capture_attempt,
                    subject_id,
                    condition_payload,
                    self._store_frames(frames),
                    qc,
                    self._capture_camera_metadata(
                        frames,
                        str(state.get("subject_metadata", {}).get("operator_id", "")),
                        condition.camera_code,
                        {
                            **dict(confirmations),
                            "nonce": "consumed",
                            "confirmed_for_attempt_id": attempt_id,
                            "confirmed_at": datetime.now(timezone.utc).isoformat(),
                        },
                        qc,
                    ),
                )
                committed = True
                self.active_protocol_subject_id = subject_id
                bookkeeping_status = str(
                    committed_attempt.get("bookkeeping_status") or "COMMITTED"
                )
                reconciliation_required = bookkeeping_status == "PENDING_RECONCILE"
                if reconciliation_required:
                    self._mark_protocol_reconciliation_required(subject_id)
                    protocol_state = None
                    post_commit_error = committed_attempt.get("post_commit_error")
                    logger.error(
                        "Capture files are durable but bookkeeping requires recovery: {}",
                        committed_attempt.get("recovery_error") or post_commit_error,
                    )
                else:
                    try:
                        protocol_state = self._protocol_subject_state(subject_id)
                        self._apply_protocol_distance_target(protocol_state)
                        post_commit_error = committed_attempt.get("post_commit_error")
                    except Exception as exc:
                        logger.exception("Capture committed but state refresh failed")
                        protocol_state = None
                        post_commit_error = f"{type(exc).__name__}: {exc}"
                accepted = qc["status"] == "PASS" and not reconciliation_required
                review_required = qc["status"] == "WARN" and not reconciliation_required
                needs_retake = qc["status"] == "FAIL" and not reconciliation_required
                if reconciliation_required:
                    message = (
                        "采集文件已原子落盘，但状态账本尚未恢复；请停止继续操作并重启采集服务"
                    )
                elif accepted:
                    message = "采集已通过客观质控并被接受"
                elif review_required:
                    message = "采集已安全保存，需查看已提交 F03 后完成人工复核"
                else:
                    message = "采集已保存为未通过 attempt，请按原因补采"
                review_preview = {}
                if review_required:
                    try:
                        review_preview = self._load_protocol_review_preview(
                            subject_id, condition_id, attempt_id
                        )
                    except Exception as exc:
                        logger.warning(f"Committed F03 preview verification failed: {exc}")
                result = {
                    "success": not needs_retake and not reconciliation_required,
                    "operation_success": True,
                    "committed": True,
                    "accepted": accepted,
                    "review_required": review_required,
                    "needs_retake": needs_retake,
                    "subject_id": subject_id,
                    "condition_id": condition_id,
                    "attempt_id": attempt_id,
                    "quality_status": qc["status"],
                    "qc": qc,
                    "state": protocol_state,
                    "review_preview": review_preview,
                    "bookkeeping_status": bookkeeping_status,
                    "reconciliation_required": reconciliation_required,
                    "post_commit_error": post_commit_error,
                    "error": message if reconciliation_required else None,
                    "message": message,
                }
                try:
                    await self._emit_protocol_message(
                        websocket, {"type": "protocol_capture_result", "data": result}
                    )
                    if protocol_state is not None:
                        await self._emit_protocol_message(
                            websocket,
                            {"type": "protocol_subject_state", "data": protocol_state},
                        )
                except Exception:
                    # Transport/UI refresh cannot roll back a durable capture.
                    logger.exception("Capture committed but client notification failed")
                if self.voice_synthesizer:
                    self.voice_synthesizer.speak(
                        (
                            "数据已保存，但状态待恢复，请停止操作并重启服务。"
                            if reconciliation_required
                            else "采集完成。"
                            if accepted
                            else (
                                "采集已保存，请人工复核。"
                                if review_required
                                else "质量检查未通过，请重新采集。"
                            )
                        ),
                        blocking=False,
                    )
                return result
            except Exception as exc:
                # If an exception happened after the durable rename/state
                # update, never report committed:false or abort that attempt.
                if attempt_id and not committed:
                    try:
                        recovered_state = self.protocol_store.get_subject_state(subject_id)
                        recovered_attempt = recovered_state.get("attempts", {}).get(
                            attempt_id, {}
                        )
                        committed = recovered_attempt.get("status") == "COMMITTED"
                        if committed:
                            committed_attempt = recovered_attempt
                    except Exception:
                        pass
                if committed:
                    logger.exception("Post-commit protocol processing failed")
                    review_preview = {}
                    if attempt_id:
                        try:
                            review_preview = self._load_protocol_review_preview(
                                subject_id, condition_id, attempt_id
                            )
                        except Exception as preview_exc:
                            logger.warning(
                                "Committed F03 preview recovery failed: {}",
                                preview_exc,
                            )
                    result = {
                        "success": False,
                        "operation_success": True,
                        "committed": True,
                        "accepted": bool(
                            committed_attempt
                            and committed_attempt.get("quality_status") == "PASS"
                        ),
                        "review_required": bool(
                            committed_attempt
                            and committed_attempt.get("quality_status") == "WARN"
                        ),
                        "needs_retake": bool(
                            committed_attempt
                            and committed_attempt.get("quality_status") == "FAIL"
                        ),
                        "subject_id": subject_id,
                        "condition_id": condition_id,
                        "attempt_id": attempt_id,
                        "quality_status": (
                            committed_attempt or {}
                        ).get("quality_status"),
                        "qc": qc or (committed_attempt or {}).get("qc"),
                        "review_preview": review_preview,
                        "post_commit_error": f"{type(exc).__name__}: {exc}",
                        "message": "数据已提交成功，但界面刷新失败；请刷新受试者状态",
                    }
                    try:
                        await self._emit_protocol_message(
                            websocket,
                            {"type": "protocol_capture_result", "data": result},
                        )
                    except Exception:
                        pass
                    return result
                if attempt_id:
                    try:
                        await asyncio.to_thread(
                            self.protocol_store.fail_capture_attempt,
                            subject_id,
                            condition_payload,
                            f"{type(exc).__name__}: {exc}",
                        )
                    except Exception:
                        # commit_capture_attempt already records validation and
                        # write failures; in that case no PENDING attempt remains.
                        pass
                raise
            finally:
                self.is_capturing = False

    async def _review_protocol_capture(self, websocket, data: dict):
        if websocket is not None and not data.get("subject_id"):
            raise ValueError("协议写命令必须显式携带 subject_id")
        subject_id = str(data.get("subject_id") or self.active_protocol_subject_id or "")
        condition_id = str(data.get("condition_id") or "")
        attempt_id = str(data.get("attempt_id") or "")
        decision = str(data.get("decision") or "").upper()
        reason = str(data.get("reason") or "").strip()
        if not subject_id or not condition_id or not attempt_id:
            raise ValueError("人工复核必须指定 subject_id、condition_id 和 attempt_id")
        self._assert_protocol_subject_writable(subject_id)
        if decision not in {"ACCEPT", "REJECT"}:
            raise ValueError("人工复核 decision 只能是 ACCEPT 或 REJECT")
        if len(reason) < 4 or len(reason) > 500:
            raise ValueError("人工复核原因必须填写 4–500 个字符")
        evidence_token = str(data.get("evidence_token") or "")
        evidence_tokens = getattr(self, "_protocol_review_evidence_tokens", {})
        evidence_record = evidence_tokens.get((subject_id, attempt_id))
        expected_evidence_token = (
            str(evidence_record.get("token") or "")
            if isinstance(evidence_record, Mapping)
            else ""
        )
        if decision == "ACCEPT" and (
            not expected_evidence_token
            or not secrets.compare_digest(evidence_token, expected_evidence_token)
        ):
            raise ValueError("接受前必须读取并验证该 attempt 已落盘的 F03 RGB 与深度证据")
        verified_evidence = None
        if decision == "ACCEPT":
            issued_at = float(evidence_record.get("issued_monotonic") or 0.0)
            if time.monotonic() - issued_at > _REVIEW_EVIDENCE_TOKEN_TTL_SEC:
                evidence_tokens.pop((subject_id, attempt_id), None)
                raise ValueError("F03 复核证据令牌已过期，请重新读取已落盘证据")
            verified_evidence = self.protocol_store.get_verified_anchor_files(
                subject_id,
                condition_id,
                attempt_id,
                frame_index=3,
                modalities=("rgb", "depth_aligned"),
            )
            if not secrets.compare_digest(
                str(verified_evidence.get("evidence_sha256") or ""),
                str(evidence_record.get("evidence_sha256") or ""),
            ):
                evidence_tokens.pop((subject_id, attempt_id), None)
                raise ValueError("F03 复核证据在预览后发生变化，请重新读取")
        raw = self.protocol_store.get_subject_state(subject_id)
        condition = next(
            (
                item
                for item in self._subject_conditions(raw)
                if format_condition_id(item) == condition_id
            ),
            None,
        )
        if condition is None:
            raise ValueError("复核条件不属于当前受试者的协议快照")
        operator_id = str(raw.get("subject_metadata", {}).get("operator_id") or "")
        attempt = raw.get("attempts", {}).get(attempt_id, {})
        qc = attempt.get("qc", {}) if isinstance(attempt, Mapping) else {}
        review = await asyncio.to_thread(
            self.protocol_store.review_capture_attempt,
            subject_id,
            self._condition_payload(condition),
            attempt_id,
            decision,
            operator_id,
            reason,
            {
                "policy_version": "manual-warn-review-v1.0",
                "qc_policy_version": qc.get("policy_version"),
                "qc_policy_sha256": qc.get("policy_sha256"),
                "anchor_frame_reviewed": "F03",
                "verified_evidence_token_sha256": hashlib.sha256(
                    evidence_token.encode("utf-8")
                ).hexdigest()
                if evidence_token
                else None,
                "verified_evidence_sha256": (
                    verified_evidence.get("evidence_sha256")
                    if verified_evidence
                    else None
                ),
                "evidence_reverified_at": (
                    verified_evidence.get("verified_at")
                    if verified_evidence
                    else None
                ),
            },
        )
        evidence_tokens.pop((subject_id, attempt_id), None)
        bookkeeping_status = str(review.get("bookkeeping_status") or "COMMITTED")
        reconciliation_required = bookkeeping_status == "PENDING_RECONCILE"
        if reconciliation_required:
            self._mark_protocol_reconciliation_required(subject_id)
            state = None
            post_commit_error = review.get("post_commit_error")
            logger.error(
                "Review is durable but bookkeeping requires recovery: {}",
                review.get("recovery_error") or post_commit_error,
            )
        else:
            try:
                state = self._protocol_subject_state(subject_id)
                post_commit_error = review.get("post_commit_error")
            except Exception as exc:
                logger.exception("Review committed but state refresh failed")
                state = None
                post_commit_error = f"{type(exc).__name__}: {exc}"
        review_message = (
            "复核决定已落盘，但状态账本尚未恢复；请停止继续操作并重启采集服务"
            if reconciliation_required
            else (
                "已接受本次 WARN attempt，条件进入已采集状态"
                if decision == "ACCEPT"
                else "已驳回本次 WARN attempt，条件进入补采状态"
            )
        )
        result = {
            "success": not reconciliation_required,
            "operation_success": True,
            "committed": True,
            "subject_id": subject_id,
            "condition_id": condition_id,
            "attempt_id": attempt_id,
            "decision": decision,
            "review": review,
            "state": state,
            "bookkeeping_status": bookkeeping_status,
            "reconciliation_required": reconciliation_required,
            "post_commit_error": post_commit_error,
            "error": review_message if reconciliation_required else None,
            "message": review_message,
        }
        try:
            await self._emit_protocol_message(
                websocket, {"type": "protocol_review_result", "data": result}
            )
            if state is not None:
                await self._emit_protocol_message(
                    websocket, {"type": "protocol_subject_state", "data": state}
                )
        except Exception:
            logger.exception("Review committed but client notification failed")
        return result

    def _load_protocol_review_preview(
        self, subject_id: str, condition_id: str, attempt_id: str
    ) -> dict:
        import cv2

        evidence = self.protocol_store.get_verified_anchor_files(
            subject_id,
            condition_id,
            attempt_id,
            frame_index=3,
            modalities=("rgb", "depth_aligned"),
        )
        records = evidence["files"]
        decoded = {}
        for modality, record in records.items():
            payload = record["bytes"]
            image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
            if image is None:
                raise ProtocolStoreError(f"F03 {modality} 无法解码")
            decoded[modality] = image
        processor = getattr(self, "frame_processor", None) or FrameProcessor(
            preview_size=(480, 300), jpeg_quality=75
        )
        depth_scale = float(
            evidence.get("camera_metadata", {}).get(
                "depth_scale_mm_per_unit", 1.0
            )
        )
        evidence_tokens = getattr(self, "_protocol_review_evidence_tokens", None)
        if evidence_tokens is None:
            evidence_tokens = {}
            self._protocol_review_evidence_tokens = evidence_tokens
        evidence_token = secrets.token_urlsafe(24)
        evidence_tokens[(subject_id, attempt_id)] = {
            "token": evidence_token,
            "condition_id": condition_id,
            "evidence_sha256": evidence["evidence_sha256"],
            "issued_monotonic": time.monotonic(),
            "verified_at": evidence["verified_at"],
        }
        return {
            "subject_id": subject_id,
            "condition_id": condition_id,
            "attempt_id": attempt_id,
            "anchor_frame": "F03",
            "color": processor.encode_preview(decoded["rgb"], is_rgb=False),
            "depth": processor.encode_depth_preview_fast(
                decoded["depth_aligned"], depth_scale=depth_scale
            ),
            "source": "verified_committed_files",
            "evidence_token": evidence_token,
            "evidence_sha256": evidence["evidence_sha256"],
            "evidence_verified_at": evidence["verified_at"],
            "evidence_token_ttl_seconds": int(_REVIEW_EVIDENCE_TOKEN_TTL_SEC),
        }

    async def _send_protocol_review_preview(self, websocket, data: dict):
        subject_id = str(data.get("subject_id") or "")
        condition_id = str(data.get("condition_id") or "")
        attempt_id = str(data.get("attempt_id") or "")
        if not subject_id or not condition_id or not attempt_id:
            raise ValueError("复核预览必须指定 subject_id、condition_id 和 attempt_id")
        preview = await asyncio.to_thread(
            self._load_protocol_review_preview,
            subject_id,
            condition_id,
            attempt_id,
        )
        await self._emit_protocol_message(
            websocket, {"type": "protocol_review_preview", "data": preview}
        )
        return preview

    async def _save_protocol_anthropometry(self, websocket, data: dict):
        if websocket is not None and not data.get("subject_id"):
            raise ValueError("协议写命令必须显式携带 subject_id")
        subject_id = str(data.get("subject_id") or self.active_protocol_subject_id or "")
        if not subject_id:
            raise ValueError("请先创建或选择受试者")
        self._assert_protocol_subject_writable(subject_id)
        records = data.get("records")
        if not isinstance(records, list):
            raise ValueError("records 必须是测量记录数组")
        equipment = data.get("equipment") or {}
        if not isinstance(equipment, Mapping):
            raise ValueError("必须提交本次人工测量工具记录")
        subject_state = self.protocol_store.get_subject_state(subject_id)
        operator_id = str(subject_state.get("subject_metadata", {}).get("operator_id", ""))
        daily_check_id = str(equipment.get("daily_check_id") or "").strip()
        if daily_check_id:
            get_equipment_check = getattr(self.protocol_store, "get_equipment_check", None)
            daily_check = (
                get_equipment_check(operator_id, check_id=daily_check_id)
                if callable(get_equipment_check) else None
            )
            if not isinstance(daily_check, Mapping):
                raise ValueError("未找到当前操作员的器材日检记录，请重新执行日检")
            normalized_equipment = {
                **dict(daily_check.get("equipment") or {}),
                "daily_check_id": daily_check["check_id"],
                "daily_check_date": daily_check.get("check_date"),
                "daily_check_sha256": daily_check.get("sha256"),
                "used_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            normalized_equipment = {}
        snapshot = subject_state.get("protocol_snapshot")
        frozen_definitions = (
            snapshot.get("measurements") if isinstance(snapshot, Mapping) else None
        )
        if not isinstance(frozen_definitions, list) or not frozen_definitions:
            raise ProtocolStoreError("受试者协议快照缺少冻结的人体测量定义")
        definitions = {
            str(item.get("measurement_id") or "").upper(): item
            for item in frozen_definitions
            if isinstance(item, Mapping)
        }
        measurements = {}
        for record in records:
            if not isinstance(record, Mapping):
                raise ValueError("每条测量记录必须是对象")
            measurement_id = str(record.get("measurement_id", "")).upper()
            definition = definitions.get(measurement_id)
            if definition is None:
                raise ValueError(f"未知测量项: {measurement_id}")
            field_name = str(record.get("field_name", ""))
            if field_name not in set(map(str, definition.get("field_names", []))):
                raise ValueError(f"{measurement_id} 不包含字段 {field_name}")
            if field_name in measurements:
                raise ValueError(f"测量字段重复: {field_name}")
            values = [record.get("m1"), record.get("m2")]
            if record.get("m3") not in {None, ""}:
                values.append(record.get("m3"))
            measurements[field_name] = values
        saved = await asyncio.to_thread(
            self.protocol_store.save_anthropometry,
            subject_id,
            measurements,
            {
                "operator_id": operator_id,
                "source": "protocol_ui",
                "equipment": normalized_equipment,
            },
        )
        bookkeeping_status = str(saved.get("bookkeeping_status") or "COMMITTED")
        reconciliation_required = bookkeeping_status == "PENDING_RECONCILE"
        if reconciliation_required:
            self._mark_protocol_reconciliation_required(subject_id)
            state = None
            post_commit_error = saved.get("post_commit_error")
            logger.error(
                "Anthropometry is durable but bookkeeping requires recovery: {}",
                saved.get("recovery_error") or post_commit_error,
            )
        else:
            try:
                state = self._protocol_subject_state(subject_id)
                post_commit_error = saved.get("post_commit_error")
            except Exception as exc:
                logger.exception("Anthropometry committed but state refresh failed")
                state = None
                post_commit_error = f"{type(exc).__name__}: {exc}"
        anthropometry_message = (
            "人体测量 revision 已落盘，但状态账本尚未恢复；请停止继续操作并重启采集服务"
            if reconciliation_required
            else "人体测量已保存"
        )
        result = {
            "success": not reconciliation_required,
            "operation_success": True,
            "committed": True,
            "record": saved,
            "state": state,
            "bookkeeping_status": bookkeeping_status,
            "reconciliation_required": reconciliation_required,
            "post_commit_error": post_commit_error,
            "error": anthropometry_message if reconciliation_required else None,
            "message": anthropometry_message,
        }
        try:
            await self._emit_protocol_message(websocket, {
                "type": "anthropometry_result",
                "data": result,
            })
            if state is not None:
                await self._emit_protocol_message(
                    websocket, {"type": "protocol_subject_state", "data": state}
                )
        except Exception:
            logger.exception("Anthropometry committed but client notification failed")
        return result

    async def _save_daily_equipment_check(self, websocket, data: dict):
        subject_id = str(data.get("subject_id") or self.active_protocol_subject_id or "")
        if not subject_id:
            raise ValueError("请先选择受试者，再保存该操作员的器材日检")
        state = self.protocol_store.get_subject_state(subject_id)
        operator_id = str(state.get("subject_metadata", {}).get("operator_id", ""))
        if not _OPERATOR_ID_PATTERN.fullmatch(operator_id):
            raise ValueError("当前受试者缺少有效操作员编号")
        save_equipment_check = getattr(self.protocol_store, "save_equipment_check", None)
        if not callable(save_equipment_check):
            raise ValueError("当前存储版本不支持器材日检记录")
        equipment = data.get("equipment")
        if not isinstance(equipment, Mapping):
            raise ValueError("必须提交器材日检工具记录")
        check = await asyncio.to_thread(
            save_equipment_check,
            operator_id,
            equipment,
            note=str(data.get("note") or ""),
        )
        public_state = self._protocol_subject_state(subject_id)
        result = {
            "success": True,
            "message": "器材日检已保存，今天的同一操作员可直接复用",
            "check": check,
            "state": public_state,
        }
        if websocket is not None:
            await self._emit_protocol_message(websocket, {
                "type": "daily_equipment_check_result", "data": result,
            })
            await self._emit_protocol_message(websocket, {
                "type": "protocol_subject_state", "data": public_state,
            })
        return result

    def _dual_adapters(self):
        registry = self.camera_registry.adapters
        return registry["orbbec"], registry["realsense"]

    def _dual_session_state(self) -> dict:
        return self.dual_workflow.public_state()

    async def _create_dual_session(self, websocket, data: dict):
        subject_id = validate_subject_id(str(data.get("subject_id") or "").strip().upper())
        output_path = str(data.get("output_path") or "").strip()
        if not output_path:
            raise ValueError("请选择数据输出文件夹")
        try:
            distance = data.get("target_distance_mm")
            target_distance_mm = int(distance) if distance not in {None, ""} else None
        except (TypeError, ValueError) as exc:
            raise ValueError("距离必须是毫米整数") from exc
        state = await asyncio.to_thread(
            self.dual_workflow.create_session,
            subject_id=subject_id,
            output_path=output_path,
            clothing_note=str(data.get("clothing_note") or ""),
            target_distance_mm=target_distance_mm,
        )
        result = {"success": True, "state": state}
        await self._emit_protocol_message(websocket, {
            "type": "dual_session_state", "data": {**result["state"], "event": "created"},
        })
        return result

    async def _open_dual_session(self, websocket, data: dict):
        subject_id = validate_subject_id(str(data.get("subject_id") or "").strip().upper())
        output_path = str(data.get("output_path") or "").strip()
        if not output_path:
            raise ValueError("请选择原任务的数据输出文件夹")
        state = await asyncio.to_thread(
            self.dual_workflow.open_session,
            subject_id=subject_id,
            output_path=output_path,
        )
        await self._emit_protocol_message(websocket, {
            "type": "dual_session_state", "data": {**state, "event": "opened"},
        })
        return state

    async def _save_dual_anthropometry(self, websocket, data: dict):
        subject_id = str(data.get("subject_id") or "")
        records = data.get("records")
        if not isinstance(records, list):
            raise ValueError("records 必须是测量记录数组")
        definitions = [asdict(item) for item in measurement_definitions()]
        state = await asyncio.to_thread(
            self.dual_workflow.save_anthropometry,
            subject_id=subject_id,
            records=records,
            definitions=definitions,
        )
        result = {"success": True, "state": state, "message": "人体测量已保存"}
        await self._emit_protocol_message(websocket, {
            "type": "dual_anthropometry_result", "data": result,
        })
        return result

    async def _complete_dual_session(self, websocket, data: dict):
        subject_id = str(data.get("subject_id") or "")
        state = await asyncio.to_thread(
            self.dual_workflow.complete_session,
            subject_id=subject_id,
        )
        result = {
            "success": True,
            "state": state,
            "report": {
                "status": "COMPLETE",
                "ready_to_complete": True,
                "integrity_errors": [],
            },
            "message": "受试者双机八角度任务已完成",
        }
        await self._emit_protocol_message(websocket, {
            "type": "dual_completion_result", "data": result,
        })
        return result

    async def _capture_dual_group(self, websocket, data: dict):
        subject_id = str(data.get("subject_id") or "")
        distance = data.get("distance_mm")
        try:
            distance_mm = int(distance) if distance not in {None, ""} else None
            yaw_deg = int(data.get("yaw_deg"))
        except (TypeError, ValueError) as exc:
            raise ValueError("角度和距离必须是整数") from exc

        def set_capturing(value: bool) -> None:
            self.is_capturing = value

        def announce() -> None:
            if self.voice_synthesizer:
                self.voice_synthesizer.speak(
                    "请保持姿势不动，两秒后双机采集。", blocking=False
                )

        result = await self.dual_workflow.capture_group(
            subject_id=subject_id,
            yaw_deg=yaw_deg,
            distance_mm=distance_mm,
            ready=bool(data.get("ready")),
            capture_lock=self.capture_lock,
            camera_lock=self.camera_lock,
            set_capturing=set_capturing,
            announce=announce,
            frame_count=_PROTOCOL_BURST_FRAMES,
            interval_ms=_PROTOCOL_BURST_INTERVAL_SEC * 1000.0,
        )
        await self._emit_protocol_message(websocket, {
            "type": "dual_capture_result", "data": result,
        })
        await self._emit_protocol_message(websocket, {
            "type": "dual_session_state", "data": {**result["state"], "event": "updated"},
        })
        return result

    async def _complete_protocol_subject(self, websocket, data: dict):
        if websocket is not None and not data.get("subject_id"):
            raise ValueError("协议写命令必须显式携带 subject_id")
        subject_id = str(data.get("subject_id") or self.active_protocol_subject_id or "")
        if not subject_id:
            raise ValueError("请先创建或选择受试者")
        try:
            self._assert_protocol_subject_writable(subject_id)
            report = await asyncio.to_thread(self.protocol_store.complete_subject, subject_id)
            report_status = str(report.get("status") or "").upper()
            bookkeeping_status = str(report.get("bookkeeping_status") or "")
            reconciliation_required = bookkeeping_status == "REPORT_PENDING_REBUILD"
            if reconciliation_required:
                self._mark_protocol_reconciliation_required(subject_id)
            success = (
                report_status == "COMPLETE"
                and bool(report.get("ready_to_complete"))
                and not reconciliation_required
            )
            try:
                state = self._protocol_subject_state(subject_id)
                post_commit_error = report.get("post_commit_error")
            except Exception as state_exc:
                logger.exception("Completion committed but state refresh failed")
                state = None
                post_commit_error = f"{type(state_exc).__name__}: {state_exc}"
            result = {
                "success": success,
                "operation_success": True,
                "committed": report_status
                in {"COMPLETE", "CORRUPTED", "REPORT_PENDING_REBUILD"},
                "reconciliation_required": reconciliation_required,
                "bookkeeping_status": bookkeeping_status or None,
                "report": report,
                "state": state,
                "post_commit_error": post_commit_error,
                **(
                    {}
                    if success
                    else {
                        "error": (
                            "完成状态已落盘但报告待重建；请停止操作并重启采集服务"
                            if reconciliation_required
                            else "数据完整性复核失败，受试者已标记为 CORRUPTED"
                            if report_status == "CORRUPTED"
                            else "完成门禁未通过"
                        )
                    }
                ),
            }
        except IncompleteSubjectError as exc:
            try:
                state = self._protocol_subject_state(subject_id)
            except Exception:
                state = None
            result = {
                "success": False,
                "operation_success": True,
                "committed": False,
                "error": "条件采集或必填人工测量尚未闭环",
                "report": exc.report,
                "state": state,
            }
        except ProtocolStoreError as exc:
            try:
                state = self._protocol_subject_state(subject_id)
            except Exception:
                state = None
            result = {
                "success": False,
                "operation_success": False,
                "committed": False,
                "reconciliation_required": self._protocol_reconciliation_required(
                    subject_id
                ),
                "error": str(exc),
                "report": None,
                "state": state,
            }
        try:
            await self._emit_protocol_message(
                websocket, {"type": "protocol_completion_result", "data": result}
            )
            if state is not None:
                await self._emit_protocol_message(
                    websocket, {"type": "protocol_subject_state", "data": state}
                )
            if websocket is not None:
                await self._send_protocol_subjects(websocket)
        except Exception:
            logger.exception("Completion result could not be delivered to client")
        return result

    async def _send_error(self, websocket, message: str):
        try:
            await websocket.send(json.dumps({"type": "error", "message": message}, ensure_ascii=False))
        except Exception:
            pass

    def _speak_auto(self, key: str, text: str):
        if self.auto_last_voice_key == key:
            return
        self.auto_last_voice_key = key
        if self.voice_synthesizer:
            self.voice_synthesizer.speak(text, blocking=False)

    async def _broadcast_auto_status(self):
        await self._broadcast({
            "type": "auto_capture_status",
            "data": {
                "enabled": self.auto_capture_enabled,
                "stable_frames": len(self.auto_stable_distances),
                "required_frames": self.auto_required_frames,
                "captured": self.auto_captured_count,
                "target_count": self.auto_target_count,
                "state": self.auto_state,
                "message": self.auto_message
            }
        })

    def _set_auto_waiting(self, state: str, message: str, reset_stability: bool = True):
        if reset_stability:
            self.auto_stable_distances = []
        self.auto_state = state
        self.auto_message = message

    async def _start_auto_capture(self, data: dict = None):
        if not _LEGACY_WRITES_ENABLED:
            self.auto_capture_enabled = False
            self.auto_state = "disabled"
            self.auto_message = "旧版自动连拍已停用"
            await self._broadcast_auto_status()
            return False
        data = data or {}
        self.auto_capture_options = data.get("options") or {}
        self.auto_required_frames = min(
            _MAX_AUTO_STABLE_FRAMES,
            max(1, int(data.get("stable_frames", 10)))
        )
        self.auto_max_distance_delta_mm = min(
            _MAX_AUTO_DISTANCE_DELTA_MM,
            max(1.0, float(data.get("max_distance_delta_mm", 30.0)))
        )
        self.auto_target_count = min(
            _MAX_AUTO_CAPTURE_COUNT,
            max(1, int(data.get("capture_count", 3)))
        )
        self.auto_capture_interval_sec = min(
            _MAX_AUTO_CAPTURE_INTERVAL_SEC,
            max(0.1, float(data.get("capture_interval_sec", 1.0)))
        )
        self.auto_capture_enabled = True
        self.auto_stable_distances = []
        self.auto_captured_count = 0
        self.auto_state = "waiting"
        self.auto_message = "自动采集已开启，请站到相机前方"
        self.auto_last_voice_key = None
        self._speak_auto("started", "自动采集已开启，请站到相机前方。")
        await self._broadcast_auto_status()

    async def _stop_auto_capture(self, speak: bool = True):
        self.auto_capture_enabled = False
        self.auto_stable_distances = []
        self.auto_state = "stopped"
        self.auto_message = "自动采集已停止"
        if speak:
            self.auto_last_voice_key = None
            self._speak_auto("stopped", "自动采集已停止。")
        await self._broadcast_auto_status()

    async def _update_auto_capture(self, distance_info):
        if not self.auto_capture_enabled:
            return
        if self.auto_task and not self.auto_task.done():
            return

        if distance_info is None or distance_info.status != DistanceStatus.OPTIMAL:
            if self.auto_state != "waiting":
                self.auto_last_voice_key = None
            self._set_auto_waiting("waiting", "等待人体进入合适距离")
            await self._broadcast_auto_status()
            return

        distance_mm = float(distance_info.distance_mm)
        self.auto_stable_distances.append(distance_mm)
        if len(self.auto_stable_distances) > self.auto_required_frames:
            self.auto_stable_distances = self.auto_stable_distances[-self.auto_required_frames:]

        distance_delta = max(self.auto_stable_distances) - min(self.auto_stable_distances)
        if distance_delta > self.auto_max_distance_delta_mm:
            self.auto_stable_distances = [distance_mm]
            self.auto_state = "stabilizing"
            self.auto_message = "距离合适，请保持不动"
            self._speak_auto("hold_still", "距离合适，请保持不动。")
            await self._broadcast_auto_status()
            return

        self.auto_state = "stabilizing"
        self.auto_message = "距离合适，请保持不动"
        self._speak_auto("hold_still", "距离合适，请保持不动。")
        await self._broadcast_auto_status()

        if len(self.auto_stable_distances) >= self.auto_required_frames:
            self.auto_task = asyncio.create_task(self._run_auto_capture_batch())

    async def _run_auto_capture_batch(self):
        self.auto_state = "capturing"
        self.auto_message = "姿态稳定，开始自动采集"
        self.auto_stable_distances = []
        self._speak_auto("batch_started", "姿态稳定，开始自动采集。")
        await self._broadcast_auto_status()

        while self.auto_capture_enabled and self.auto_captured_count < self.auto_target_count:
            capture_options = {**self.auto_capture_options, "_suppress_voice": True}
            result = await self._handle_capture(capture_options)
            next_index = self.auto_captured_count + 1
            if result and result.success:
                self.auto_captured_count = next_index
                self.auto_message = f"已采集第 {self.auto_captured_count} 组"
                self._speak_auto(f"capture_success_{self.auto_captured_count}", f"已采集第 {self.auto_captured_count} 组。")
            else:
                self.auto_message = f"第 {next_index} 组采集失败，请保持姿态"
                self._speak_auto(f"capture_failed_{next_index}", f"第 {next_index} 组采集失败，请保持姿态。")

            await self._broadcast_auto_status()
            if self.auto_captured_count >= self.auto_target_count:
                break
            await asyncio.sleep(self.auto_capture_interval_sec)

        if self.auto_capture_enabled and self.auto_captured_count >= self.auto_target_count:
            self.auto_capture_enabled = False
            self.auto_state = "completed"
            self.auto_message = f"自动采集完成，共采集 {self.auto_captured_count} 组数据"
            self.auto_last_voice_key = None
            self._speak_auto("completed", f"自动采集完成，共采集 {self.auto_captured_count} 组数据。")
            await self._broadcast_auto_status()

    async def _handle_capture(self, options: dict = None):
        if not _LEGACY_WRITES_ENABLED:
            await self._broadcast({
                "type": "capture_result",
                "data": {
                    "success": False,
                    "error": "旧版采集写入已停用，请使用 RealAnthro 条件采集",
                },
            })
            return None
        if getattr(self, "is_shutting_down", False):
            await self._broadcast({
                "type": "capture_result",
                "data": {"success": False, "error": "系统正在安全关闭"},
            })
            return None
        if self.capture_lock.locked():
            await self._broadcast({
                "type": "capture_result",
                "data": {"success": False, "error": "正在采集中，请稍候"}
            })
            return None

        async with self.capture_lock:
            self.is_capturing = True
            try:
                suppress_voice = bool((options or {}).get("_suppress_voice"))
                config = self._build_capture_config(options)
                if self.voice_synthesizer and not suppress_voice:
                    self.voice_synthesizer.speak("开始采集，请保持姿势不动。", blocking=False)

                await asyncio.sleep(1)

                async with self.camera_lock:
                    frames = await asyncio.to_thread(
                        self.active_camera_adapter.get_frames,
                        1000,
                    )
                if not frames:
                    await self._broadcast({
                        "type": "capture_result",
                        "data": {"success": False, "error": "未获取到相机画面"}
                    })
                    return None

                if frames.depth is not None:
                    human_detected, _ = self.depth_analyzer.detect_human(frames.depth, frames.depth_scale)
                    if not human_detected:
                        if self.voice_synthesizer and not suppress_voice:
                            self.voice_synthesizer.speak("未识别到人体，请站在相机前方。", blocking=False)
                        await self._broadcast({
                            "type": "capture_result",
                            "data": {
                                "success": False,
                                "error": "未识别到人体，请站在相机前方"
                            }
                        })
                        return None

                point_cloud = None
                if config.save_pointcloud:
                    point_cloud = self.camera.generate_point_cloud(
                        frames,
                        colored=config.colored_pointcloud,
                        stride=self.settings.storage.pointcloud_stride
                    )

                intrinsics = self.camera.get_camera_intrinsics()
                result = self.data_collector.capture(frames, point_cloud, config, camera_intrinsics=intrinsics)

                await self._broadcast({
                    "type": "capture_result",
                    "data": {
                        "session_id": result.session_id,
                        "capture_id": result.capture_id,
                        "success": result.success,
                        "rgb_path": result.rgb_path,
                        "depth_path": result.depth_path,
                        "pointcloud_path": result.pointcloud_path,
                        "error": result.error
                    }
                })

                if self.voice_synthesizer and not suppress_voice:
                    if result.success:
                        self.voice_synthesizer.speak("采集完成。", blocking=False)
                    else:
                        self.voice_synthesizer.speak("采集失败，请重试。", blocking=False)
                return result
            finally:
                self.is_capturing = False

    async def _handle_finish(self):
        if not _LEGACY_WRITES_ENABLED:
            await self._broadcast({
                "type": "error",
                "message": "旧版会话写入已停用",
            })
            return False
        if self.voice_synthesizer:
            count = self.data_collector.get_capture_count()
            self.voice_synthesizer.speak(f"采集完成，共采集{count}组数据。", blocking=False)

        self.data_collector.close_session()
        await self._broadcast({
            "type": "session_finished",
            "data": {"capture_count": self.data_collector.get_capture_count()}
        })

    async def _broadcast_voice_activity(self, is_active: bool):
        """Broadcast voice activity status"""
        await self._broadcast({
            "type": "voice_activity",
            "data": {"active": is_active}
        })

    def _camera_status_snapshot(self, action: str = "status") -> dict:
        """Build a camera status snapshot while the caller owns camera_lock."""
        adapter = self.active_camera_adapter
        devices = self.camera_registry.list_devices()
        active_devices = [
            device
            for device in devices
            if device.get("backend") == adapter.backend
        ]
        status = dict(adapter.get_status(devices=active_devices))
        status["devices"] = devices
        connected_cameras = []
        for candidate in self.camera_registry.adapters.values():
            candidate_status = candidate.get_status()
            if candidate_status.get("connected"):
                connected_cameras.append({
                    "backend": candidate.backend,
                    "device": candidate_status.get("device") or {},
                    "message": candidate_status.get("message", ""),
                })
        status["connected_cameras"] = connected_cameras
        status["dual_ready"] = {
            str(item["device"].get("camera_code") or "")
            for item in connected_cameras
        } >= {"C336L", "CD435I"}
        status["active_backend"] = adapter.backend
        status["orientation"] = (
            getattr(getattr(self, "camera", None), "orientation", "landscape")
            if adapter.backend == "orbbec"
            else "landscape"
        )
        status["action"] = action
        return status

    async def _send_camera_operation(self, state: str):
        await self._broadcast({
            "type": "camera_operation",
            "data": {"state": state},
        })

    async def _send_camera_status(
        self,
        websocket=None,
        action: str = "status",
        status: Optional[dict] = None,
    ):
        if status is None:
            async with self.camera_lock:
                status = await asyncio.to_thread(
                    self._camera_status_snapshot,
                    action,
                )
        else:
            status = dict(status)
            status["action"] = action
        message = {
            "type": "camera_status",
            "data": status
        }
        if websocket is not None and websocket not in self.clients:
            try:
                await websocket.send(json.dumps(message, ensure_ascii=False))
            except Exception:
                pass
            return
        await self._broadcast(message)

    async def _handle_connect_camera(self, websocket=None, data: dict = None):
        if getattr(self, "is_shutting_down", False):
            await self._send_error(websocket, "系统正在安全关闭，拒绝连接摄像头")
            return
        if self.capture_lock.locked():
            await self._send_error(websocket, "正在采集中，请采集结束后再连接摄像头")
            return

        async with self.camera_operation_lock:
            await self._send_camera_operation("connecting")
            if self.capture_lock.locked():
                await self._send_camera_operation("idle")
                await self._send_error(websocket, "正在采集中，请采集结束后再切换摄像头")
                return

            data = data or {}
            device_id = str(data.get("device_id", "")).strip()
            async with self.camera_lock:
                if self.capture_lock.locked():
                    operation_error = "正在采集中，请采集结束后再切换摄像头"
                    status = None
                    ok = False
                else:
                    operation_error = ""
                    devices = await asyncio.to_thread(
                        self.camera_registry.list_devices
                    )
                    if not device_id and devices:
                        device_id = devices[0]["id"]
                    if ":" not in device_id:
                        match = next(
                            (
                                item["id"]
                                for item in devices
                                if device_id
                                in {
                                    str(item.get("serial_number", "")),
                                    str(item.get("uid", "")),
                                    str(item.get("index", "")),
                                }
                            ),
                            None,
                        )
                        if match:
                            device_id = match

                    if not device_id:
                        ok = False
                    else:
                        adapter = self.camera_registry.for_device(device_id)
                        height = (
                            720
                            if adapter.backend == "realsense"
                            else self.settings.camera.height
                        )
                        ok = await asyncio.to_thread(
                            adapter.connect,
                            device_id=device_id,
                            width=self.settings.camera.width,
                            height=height,
                            fps=self.settings.camera.fps,
                            params_file=self.settings.camera.params_file,
                            enable_infrared=False,
                        )
                        self.active_camera_adapter = adapter
                        if ok:
                            self.depth_analyzer.reset()
                            self._last_color_preview = ""
                            self._last_depth_preview = ""
                            self._preview_miss_count = 0
                    status = await asyncio.to_thread(
                        self._camera_status_snapshot,
                        "connect",
                    )

            if operation_error:
                await self._send_camera_operation("idle")
                await self._send_error(websocket, operation_error)
                return
            if ok:
                logger.info(f"Camera connected by client request: {device_id}")
            else:
                logger.warning(
                    "Camera connect failed: {}",
                    status.get("message") or "未检测到可连接的摄像头",
                )
            await self._send_camera_status(
                websocket,
                action="connect",
                status=status,
            )
            if ok:
                await self._ensure_preview_task()

    async def _handle_disconnect_camera(self, websocket=None):
        if getattr(self, "is_shutting_down", False):
            await self._send_error(websocket, "系统正在安全关闭")
            return
        if self.capture_lock.locked():
            await self._send_error(websocket, "正在采集中，请采集结束后再断开摄像头")
            return

        async with self.camera_operation_lock:
            await self._send_camera_operation("disconnecting")
            if self.capture_lock.locked():
                await self._send_camera_operation("idle")
                await self._send_error(websocket, "正在采集中，请采集结束后再断开摄像头")
                return

            async with self.camera_lock:
                if self.capture_lock.locked():
                    operation_error = "正在采集中，请采集结束后再断开摄像头"
                    status = None
                else:
                    operation_error = ""
                    connected_adapters = [
                        candidate
                        for candidate in self.camera_registry.adapters.values()
                        if candidate.get_status().get("connected")
                    ]
                    for candidate in connected_adapters:
                        await asyncio.to_thread(candidate.disconnect)
                    self.active_camera_adapter = self.camera_registry.adapters["orbbec"]
                    self.depth_analyzer.reset()
                    self._last_color_preview = ""
                    self._last_depth_preview = ""
                    self._preview_miss_count = 0
                    status = await asyncio.to_thread(
                        self._camera_status_snapshot,
                        "disconnect",
                    )

            if operation_error:
                await self._send_camera_operation("idle")
                await self._send_error(websocket, operation_error)
                return
            await self._broadcast({
                "type": "preview_frame",
                "data": {
                    "color": "",
                    "depth": "",
                    "distance": {
                        "distance_mm": 0,
                        "status": DistanceStatus.NO_DATA.value,
                        "message": "摄像头未连接",
                        "confidence": 0
                    }
                }
            })
            await self._send_camera_status(
                websocket,
                action="disconnect",
                status=status,
            )

    async def _process_http_request(self, connection, request):
        if request.path == "/health":
            origin = request.headers.get("Origin")
            body = json.dumps({
                "ok": True,
                "service": "body-posture-backend",
                "host": self.host,
                "port": self.port,
                "clients": len(self.clients)
            }, ensure_ascii=False).encode("utf-8")
            headers = Headers([
                ("Content-Type", "application/json"),
                *_cors_headers_for_origin(origin)
            ])
            return Response(200, "OK", headers, body)

        if request.path == "/auth-token":
            origin = request.headers.get("Origin")
            if origin:
                if not _is_allowed_auth_origin(origin):
                    body = b"Forbidden origin"
                    headers = Headers([("Content-Type", "text/plain; charset=utf-8")])
                    return Response(403, "Forbidden", headers, body)
                cors_headers = _cors_headers_for_origin(origin)
            elif not _is_local_connection(connection):
                body = b"Forbidden"
                headers = Headers([("Content-Type", "text/plain; charset=utf-8")])
                return Response(403, "Forbidden", headers, body)
            else:
                cors_headers = []

            body = json.dumps({"token": self.auth_token}).encode("utf-8")
            headers = Headers([
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-store, max-age=0"),
                ("Pragma", "no-cache"),
                *cors_headers,
            ])
            return Response(200, "OK", headers, body)
        if request.headers.get("Upgrade", "").lower() != "websocket":
            origin = request.headers.get("Origin")
            body = json.dumps({
                "ok": False,
                "service": "body-posture-backend",
                "message": "Use /health to check backend status, or connect with WebSocket."
            }, ensure_ascii=False).encode("utf-8")
            headers = Headers([
                ("Content-Type", "application/json"),
                *_cors_headers_for_origin(origin)
            ])
            return Response(404, "Not Found", headers, body)
        return None

    async def _handle_client(self, websocket):
        try:
            remote = websocket.remote_address
        except Exception:
            remote = "unknown"

        try:
            auth_msg = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            data = json.loads(auth_msg)
            if data.get("type") != "auth" or data.get("token") != self.auth_token:
                await websocket.close(4001, "Authentication failed")
                logger.warning(f"Authentication failed from {remote}")
                return
        except (
            asyncio.TimeoutError,
            json.JSONDecodeError,
            websockets.exceptions.ConnectionClosedOK,
            websockets.exceptions.ConnectionClosedError,
        ):
            try:
                await websocket.close(4001, "Authentication timeout")
            except Exception:
                pass
            logger.warning(f"Authentication timeout from {remote}")
            return

        self.clients.add(websocket)
        self._had_authenticated_client = True
        if self._idle_shutdown_task and not self._idle_shutdown_task.done():
            self._idle_shutdown_task.cancel()
        self._idle_shutdown_task = None
        logger.info(f"Client connected: {remote}")
        try:
            await websocket.send(json.dumps({"type": "auth_success"}, ensure_ascii=False))
        except Exception:
            self.clients.discard(websocket)
            return

        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    await self._process_message(websocket, data)
                except json.JSONDecodeError:
                    await self._send_error(websocket, "请求格式无效")
                except Exception as e:
                    logger.error(f"Failed to process message: {e}")
                    safe_msg = _ERROR_MESSAGES.get(type(e), "服务器内部错误，请稍后重试")
                    try:
                        await self._send_error(websocket, safe_msg)
                    except Exception:
                        pass
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.debug(f"Client connection error: {e}")
        finally:
            self.clients.discard(websocket)
            logger.info(f"Client disconnected: {remote}")
            if len(self.clients) == 0:
                try:
                    await self._stop_preview()
                except Exception:
                    pass
                if (
                    self.shutdown_when_idle
                    and self._had_authenticated_client
                    and not self.is_shutting_down
                ):
                    if (
                        self._idle_shutdown_task is None
                        or self._idle_shutdown_task.done()
                    ):
                        self._idle_shutdown_task = asyncio.create_task(
                            self._shutdown_after_idle()
                        )

    async def _shutdown_after_idle(self, delay: float = 5.0):
        """Stop browser-launched services only after active transactions finish."""
        try:
            await asyncio.sleep(delay)
            if (
                self.clients
                or self.is_shutting_down
                or not getattr(self, "_had_authenticated_client", False)
            ):
                return

            deadline = time.monotonic() + 120.0
            while self.capture_lock.locked() or self.camera_operation_lock.locked():
                if self.clients or self.is_shutting_down:
                    return
                if time.monotonic() >= deadline:
                    logger.warning(
                        "Idle shutdown skipped because a capture or camera operation is still active"
                    )
                    return
                await asyncio.sleep(0.25)

            logger.info("No frontend client reconnected; stopping application services")
            self.is_shutting_down = True
            if getattr(self, "_ws_server", None):
                self._ws_server.close()
        except asyncio.CancelledError:
            pass

    async def _process_message(self, websocket, data: dict):
        msg_type = data.get("type")

        if msg_type == "get_protocol_catalog":
            await websocket.send(json.dumps({
                "type": "protocol_catalog",
                "data": self._protocol_catalog(),
            }, ensure_ascii=False))
        elif msg_type == "get_protocol_subjects":
            await self._send_protocol_subjects(websocket)
        elif msg_type == "select_output_directory":
            try:
                selected = await asyncio.to_thread(_choose_native_output_directory)
                response = {"success": True, "path": selected}
            except Exception as exc:
                response = {"success": False, "error": str(exc)}
            await websocket.send(json.dumps({
                "type": "output_directory_selected", "data": response,
            }, ensure_ascii=False))
        elif msg_type == "create_dual_session":
            try:
                await self._create_dual_session(websocket, data)
            except Exception as exc:
                await websocket.send(json.dumps({
                    "type": "dual_session_state", "data": {"success": False, "error": str(exc)},
                }, ensure_ascii=False))
        elif msg_type == "open_dual_session":
            try:
                await self._open_dual_session(websocket, data)
            except Exception as exc:
                await websocket.send(json.dumps({
                    "type": "dual_session_state", "data": {"success": False, "error": str(exc)},
                }, ensure_ascii=False))
        elif msg_type == "capture_dual_group":
            try:
                await self._capture_dual_group(websocket, data)
            except Exception as exc:
                await websocket.send(json.dumps({
                    "type": "dual_capture_result", "data": {"success": False, "error": str(exc)},
                }, ensure_ascii=False))
        elif msg_type == "save_dual_anthropometry":
            try:
                await self._save_dual_anthropometry(websocket, data)
            except Exception as exc:
                await websocket.send(json.dumps({
                    "type": "dual_anthropometry_result", "data": {"success": False, "error": str(exc)},
                }, ensure_ascii=False))
        elif msg_type == "complete_dual_session":
            try:
                await self._complete_dual_session(websocket, data)
            except Exception as exc:
                await websocket.send(json.dumps({
                    "type": "dual_completion_result", "data": {"success": False, "error": str(exc)},
                }, ensure_ascii=False))
        elif msg_type == "create_protocol_subject":
            await self._create_protocol_subject(websocket, data)
        elif msg_type == "select_protocol_subject":
            await self._select_protocol_subject(websocket, data)
        elif msg_type == "set_protocol_preview_condition":
            await self._set_protocol_preview_condition(websocket, data)
        elif msg_type == "capture_protocol_condition":
            try:
                await self._capture_protocol_condition(websocket, data)
            except Exception as exc:
                logger.warning(f"Protocol capture rejected: {exc}")
                state = None
                subject_id = str(data.get("subject_id") or "")
                if subject_id:
                    try:
                        state = self._protocol_subject_state(subject_id)
                    except Exception:
                        pass
                await websocket.send(json.dumps({
                    "type": "protocol_capture_result",
                    "data": {
                        "success": False,
                        "operation_success": False,
                        "committed": False,
                        "error": str(exc),
                        "state": state,
                    },
                }, ensure_ascii=False))
                if state is not None:
                    await websocket.send(json.dumps({
                        "type": "protocol_subject_state",
                        "data": state,
                    }, ensure_ascii=False))
        elif msg_type == "protocol_review_capture":
            try:
                await self._review_protocol_capture(websocket, data)
            except Exception as exc:
                logger.warning(f"Protocol review rejected: {exc}")
                await websocket.send(json.dumps({
                    "type": "protocol_review_result",
                    "data": {"success": False, "error": str(exc)},
                }, ensure_ascii=False))
        elif msg_type == "get_protocol_review_preview":
            try:
                await self._send_protocol_review_preview(websocket, data)
            except Exception as exc:
                logger.warning(f"Protocol review preview rejected: {exc}")
                await websocket.send(json.dumps({
                    "type": "protocol_review_preview",
                    "data": {"success": False, "error": str(exc)},
                }, ensure_ascii=False))
        elif msg_type == "save_anthropometry":
            try:
                await self._save_protocol_anthropometry(websocket, data)
            except Exception as exc:
                logger.warning(f"Anthropometry rejected: {exc}")
                await websocket.send(json.dumps({
                    "type": "anthropometry_result",
                    "data": {"success": False, "error": str(exc)},
                }, ensure_ascii=False))
        elif msg_type == "save_daily_equipment_check":
            try:
                await self._save_daily_equipment_check(websocket, data)
            except Exception as exc:
                logger.warning(f"Daily equipment check rejected: {exc}")
                await websocket.send(json.dumps({
                    "type": "daily_equipment_check_result",
                    "data": {"success": False, "error": str(exc)},
                }, ensure_ascii=False))
        elif msg_type == "complete_protocol_subject":
            await self._complete_protocol_subject(websocket, data)
        elif msg_type == "start_preview":
            await self._start_preview(websocket)
        elif msg_type == "stop_preview":
            await self._stop_preview()
        elif msg_type == "capture_single":
            await self._send_error(
                websocket,
                "旧版单帧采集已停用，请使用 RealAnthro 当前条件的五帧采集",
            )
            return
        elif msg_type == "connect_camera":
            await self._handle_connect_camera(websocket, data)
        elif msg_type == "disconnect_camera":
            await self._handle_disconnect_camera(websocket)
        elif msg_type == "get_camera_status":
            await self._send_camera_status(websocket, action="status")
        elif msg_type == "set_camera_orientation":
            if self.active_camera_adapter.backend != "orbbec":
                await self._send_error(websocket, "当前方向设置仅用于 Gemini 预览")
                return
            if self.capture_lock.locked():
                await self._send_error(websocket, "正在采集中，不能修改相机方向")
                return
            orientation = self.camera.set_orientation(data.get("orientation"))
            self.frame_processor.preview_size = self._preview_size_for_orientation(
                orientation
            )
            self.settings.camera.orientation = orientation
            config_path = Path(__file__).resolve().parents[2] / "config.json"
            save_settings(self.settings, str(config_path))
            self.depth_analyzer.reset()
            self._last_color_preview = ""
            self._last_depth_preview = ""
            self._preview_miss_count = 0
            await self._send_camera_status(websocket, action="orientation")
        elif msg_type == "start_auto_capture":
            await self._send_error(
                websocket,
                "旧版自动连拍已停用，请按 RealAnthro 条件矩阵逐项采集",
            )
            return
        elif msg_type == "stop_auto_capture":
            await self._stop_auto_capture()
        elif msg_type == "create_session":
            await self._send_error(
                websocket,
                "旧版会话创建已停用，请新建 RealAnthro 协议受试者",
            )
            return
        elif msg_type == "get_distance":
            if self.capture_lock.locked():
                await self._send_error(websocket, "协议 burst 采集中，暂不读取额外距离帧")
                return
            async with self.camera_lock:
                depth_frame = await asyncio.to_thread(
                    self.active_camera_adapter.get_frames, 1000
                )
            if depth_frame and depth_frame.depth is not None:
                distance_info = await asyncio.to_thread(
                    self.depth_analyzer.analyze_distance,
                    depth_frame.depth,
                    depth_frame.color,
                    depth_frame.depth_scale,
                )
                try:
                    await websocket.send(json.dumps({
                        "type": "distance_update",
                        "data": self._distance_payload(distance_info)
                    }))
                except Exception:
                    pass
        elif msg_type == "speak":
            text = _validate_field(data.get("text", ""), "text")
            if self.voice_synthesizer:
                self.voice_synthesizer.speak(text, blocking=False)
        elif msg_type == "finish_session":
            if self.active_protocol_subject_id:
                await self._complete_protocol_subject(websocket, data)
            else:
                await self._send_error(
                    websocket,
                    "旧版会话完成写入已停用，请选择 RealAnthro 协议受试者",
                )
                return
        elif msg_type == "get_sessions":
            sessions = self.data_collector.get_session_list()
            try:
                await websocket.send(json.dumps({
                    "type": "session_list",
                    "data": {"sessions": sessions}
                }, ensure_ascii=False))
            except Exception:
                pass
        elif msg_type == "get_captures":
            captures = self.data_collector.get_captures()
            try:
                await websocket.send(json.dumps({
                    "type": "capture_list",
                    "data": {"captures": captures, "count": len(captures)}
                }, ensure_ascii=False))
            except Exception:
                pass
        elif msg_type == "get_capture_image":
            filename = _validate_field(data.get("filename", ""), "filename", _FILENAME_PATTERN)
            image_b64 = self.data_collector.get_capture_image(filename)
            try:
                await websocket.send(json.dumps({
                    "type": "capture_image",
                    "data": {"filename": filename, "image": image_b64}
                }, ensure_ascii=False))
            except Exception:
                pass
        elif msg_type == "review_capture":
            await self._send_error(
                websocket,
                "旧版样本复核写入已停用；RealAnthro 复核请使用协议复核入口",
            )
            return
        elif msg_type == "select_session":
            session_name = data.get("session_name")
            if session_name and self.data_collector.select_session(session_name):
                try:
                    await websocket.send(json.dumps({
                        "type": "session_created",
                        "data": {"session_id": session_name}
                    }, ensure_ascii=False))
                except Exception:
                    pass
            else:
                await self._send_error(websocket, f"Session not found: {session_name}")
        elif msg_type == "exit_app":
            if not _is_local_connection(websocket):
                await self._send_error(websocket, "exit_app is only allowed from localhost")
                return
            if self.capture_lock.locked():
                await self._send_error(
                    websocket, "正在写入五帧 burst，已拒绝退出；请等待本次提交完成"
                )
                return
            # No await occurs between the lock check and this gate, so another
            # websocket task cannot start a capture in the shutdown window.
            self.is_shutting_down = True
            logger.info("Exit command received from client")
            await self._broadcast({
                "type": "exit_confirm",
                "data": {"message": "系统即将关闭"}
            })
            await self._stop_preview()
            self._shutdown()
        else:
            await self._send_error(websocket, f"Unknown message type: {msg_type}")

    async def _start_preview(self, websocket):
        del websocket
        await self._ensure_preview_task()

    async def _ensure_preview_task(self):
        self.is_previewing = True
        task = getattr(self, "preview_task", None)
        if task is None or task.done():
            self.preview_task = asyncio.create_task(self._preview_loop())
        return self.preview_task

    async def _stop_preview(self):
        self.is_previewing = False
        if self.preview_task and not self.preview_task.done():
            try:
                await self.preview_task
            except (asyncio.CancelledError, Exception):
                pass
        self.preview_task = None

    def _dual_preview_ready(self) -> bool:
        try:
            gemini, d435i = self._dual_adapters()
            return bool(
                gemini.get_status().get("connected")
                and d435i.get_status().get("connected")
            )
        except Exception:
            return False

    async def _broadcast_dual_preview(self) -> bool:
        """Publish two independent preview streams when both cameras are live."""

        if not self._dual_preview_ready():
            return False
        gemini, d435i = self._dual_adapters()
        async with self.camera_lock:
            gemini_frame, d435i_frame = await asyncio.gather(
                asyncio.to_thread(gemini.get_frames, 1000),
                asyncio.to_thread(d435i.get_frames, 1000),
            )

        async def encode(frame):
            if frame is None:
                return {"color": "", "depth": "", "available": False}
            color, depth = await asyncio.gather(
                asyncio.to_thread(self.frame_processor.encode_preview_fast, frame.color, True)
                if frame.color is not None else asyncio.sleep(0, result=""),
                asyncio.to_thread(self.frame_processor.encode_depth_preview_fast, frame.depth, frame.depth_scale)
                if frame.depth is not None else asyncio.sleep(0, result=""),
            )
            return {
                "color": color,
                "depth": depth,
                "available": bool(color or depth),
                "host_timestamp_ns": frame.host_timestamp_ns,
                "frame_number": frame.frame_number,
            }

        gemini_preview, d435i_preview = await asyncio.gather(
            encode(gemini_frame), encode(d435i_frame)
        )
        if gemini_frame and gemini_frame.depth is not None:
            distance_info = await asyncio.to_thread(
                self.depth_analyzer.analyze_distance,
                gemini_frame.depth,
                gemini_frame.color,
                gemini_frame.depth_scale,
            )
            distance_data = self._distance_payload(distance_info)
        else:
            distance_data = {"distance_mm": 0, "status": DistanceStatus.NO_DATA.value, "message": "Gemini 预览无深度数据", "confidence": 0}
        await self._broadcast({
            "type": "preview_frame",
            "data": {
                "color": gemini_preview["color"],
                "depth": gemini_preview["depth"],
                "distance": distance_data,
                "cameras": {"C336L": gemini_preview, "CD435I": d435i_preview},
                "dual_ready": True,
            },
        })
        return True

    async def _preview_loop(self):
        current_task = asyncio.current_task()
        consecutive_errors = 0
        if not hasattr(self, "_last_color_preview"):
            self._last_color_preview = ""
        if not hasattr(self, "_last_depth_preview"):
            self._last_depth_preview = ""
        if not hasattr(self, "_preview_miss_count"):
            self._preview_miss_count = 0
        try:
            while self.is_previewing and self.clients:
                start_time = time.time()
                if self.is_capturing:
                    await asyncio.sleep(max(0.1, 1.0 / max(1, self.settings.gui.preview_fps)))
                    continue
                try:
                    if await self._broadcast_dual_preview():
                        consecutive_errors = 0
                        await asyncio.sleep(1.0 / max(1, self.settings.gui.preview_fps))
                        continue
                    async with self.camera_lock:
                        frames = await asyncio.to_thread(
                            self.active_camera_adapter.get_frames,
                            1000,
                        )
                    color_preview = ""
                    depth_preview = ""
                    color_frame = frames.color if frames else None
                    depth_frame = frames.depth if frames else None

                    # Installation orientation affects only the operator preview.
                    # Protocol artifacts remain in the sensor-native orientation so
                    # RGB, raw/aligned depth, IR and calibration stay consistent.
                    if (
                        frames
                        and getattr(self.active_camera_adapter, "backend", "") == "orbbec"
                        and getattr(
                            getattr(self, "camera", None),
                            "orientation",
                            "landscape",
                        ) != "landscape"
                    ):
                        color_frame = self.camera._rotate_array(color_frame)
                        depth_frame = self.camera._rotate_array(depth_frame)

                    if color_frame is not None:
                        color_preview = await asyncio.to_thread(
                            self.frame_processor.encode_preview_fast,
                            color_frame,
                            True,
                        )
                        if color_preview:
                            self._last_color_preview = color_preview

                    if frames and frames.depth is not None:
                        depth_preview, distance_info = await asyncio.gather(
                            asyncio.to_thread(
                                self.frame_processor.encode_depth_preview_fast,
                                depth_frame,
                                frames.depth_scale,
                            ),
                            asyncio.to_thread(
                                self.depth_analyzer.analyze_distance,
                                frames.depth,
                                frames.color,
                                frames.depth_scale,
                            ),
                        )
                        distance_data = self._distance_payload(distance_info)
                        if depth_preview:
                            self._last_depth_preview = depth_preview
                            self._preview_miss_count = 0
                        else:
                            self._preview_miss_count += 1
                        await self._update_auto_capture(distance_info)
                    else:
                        self._preview_miss_count += 1
                        await self._update_auto_capture(None)
                        distance_data = {
                            "distance_mm": 0,
                            "status": DistanceStatus.NO_DATA.value,
                            "message": getattr(
                                self.active_camera_adapter, "last_error", ""
                            )
                            or "摄像头未连接",
                            "confidence": 0,
                        }

                    if self._preview_miss_count <= 5:
                        color_preview = color_preview or self._last_color_preview
                        depth_preview = depth_preview or self._last_depth_preview

                    await self._broadcast({
                        "type": "preview_frame",
                        "data": {
                            "color": color_preview,
                            "depth": depth_preview,
                            "distance": distance_data,
                        },
                    })
                    consecutive_errors = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    consecutive_errors += 1
                    logger.error(
                        "Preview frame failed (attempt {}): {}",
                        consecutive_errors,
                        exc,
                    )
                    await self._broadcast({
                        "type": "preview_frame",
                        "data": {
                            "color": self._last_color_preview
                            if consecutive_errors <= 5 else "",
                            "depth": self._last_depth_preview
                            if consecutive_errors <= 5 else "",
                            "distance": {
                                "distance_mm": 0,
                                "status": DistanceStatus.NO_DATA.value,
                                "message": "预览暂时中断，正在自动重试",
                                "confidence": 0,
                            },
                        },
                    })
                    retry_delay = min(
                        1.0,
                        0.1 * (2 ** min(consecutive_errors - 1, 3)),
                    )
                    await asyncio.sleep(retry_delay)
                    continue

                elapsed = time.time() - start_time
                target_interval = 1.0 / self.settings.gui.preview_fps
                sleep_time = max(0, target_interval - elapsed)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Preview loop stopped unexpectedly: {}", exc)
        finally:
            if getattr(self, "preview_task", None) is current_task:
                self.preview_task = None

    async def _broadcast(self, message: dict):
        if not self.clients:
            return
        try:
            message_str = json.dumps(message, ensure_ascii=False)
            clients = list(self.clients)

            async def send_one(client):
                try:
                    await asyncio.wait_for(client.send(message_str), timeout=0.5)
                    return None
                except Exception:
                    return client

            closed = await asyncio.gather(*(send_one(client) for client in clients))
            for client in filter(None, closed):
                self.clients.discard(client)
                try:
                    await client.close(code=1011, reason="client too slow")
                except Exception:
                    pass
        except Exception:
            pass

    async def start(self):
        try:
            self.loop = asyncio.get_event_loop()

            token_file = os.environ.get("BODY_POSTURE_TOKEN_FILE") or os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                ".ws_token",
            )
            os.makedirs(os.path.dirname(os.path.abspath(token_file)), exist_ok=True)
            with open(token_file, 'w', encoding='utf-8') as f:
                f.write(self.auth_token)

            camera_ok = await asyncio.to_thread(
                self.active_camera_adapter.connect,
                width=self.settings.camera.width,
                height=self.settings.camera.height,
                fps=self.settings.camera.fps,
                params_file=self.settings.camera.params_file,
                enable_infrared=False,
            )

            self._ws_server = await websockets.serve(
                self._handle_client, self.host, self.port,
                process_request=self._process_http_request,
                logger=_get_websocket_logger(),
                ping_interval=10,
                ping_timeout=10,
                close_timeout=3,
                max_queue=16,
                max_size=2 * 1024 * 1024,
            )
            logger.info(f"WebSocket server started on ws://{self.host}:{self.port}")

            if self.shutdown_when_idle:
                self._idle_shutdown_task = asyncio.create_task(
                    self._shutdown_after_idle(delay=30.0)
                )

            if camera_ok:
                logger.info("Camera ready")
            else:
                logger.warning(
                    f"Camera not connected: {self.active_camera_adapter.get_status().get('message')}"
                )

            if self.voice_synthesizer:
                self.voice_synthesizer.speak("系统已启动", blocking=False)

            await self._ws_server.wait_closed()
        except Exception as e:
            logger.error(f"Server error: {e}")
            raise

    def stop(self):
        self.is_previewing = False
        self.is_capturing = False
        self.auto_capture_enabled = False
        idle_task = self._idle_shutdown_task
        if idle_task and not idle_task.done():
            idle_task.cancel()
        self._idle_shutdown_task = None
        if self.voice_recognizer:
            try:
                self.voice_recognizer.release()
            except Exception:
                pass
            self.voice_recognizer = None
        try:
            self.depth_analyzer.close()
        except Exception:
            pass
        if hasattr(self, '_ws_server') and self._ws_server:
            try:
                self._ws_server.close()
            except Exception:
                pass
        for adapter in self.camera_registry.adapters.values():
            try:
                adapter.disconnect()
            except Exception:
                pass
        protocol_store = getattr(self, "protocol_store", None)
        if protocol_store is not None and hasattr(protocol_store, "close"):
            try:
                protocol_store.close()
            except Exception:
                pass
        dual_workflow = getattr(self, "dual_workflow", None)
        if dual_workflow is not None:
            try:
                dual_workflow.close()
            except Exception:
                pass
        self.loop = None
        logger.info("Server stopped")

    def get_auth_info(self) -> dict:
        return {"token": self.auth_token, "host": self.host, "port": self.port}

    def _shutdown(self):
        logger.info("Shutting down application...")
        self.is_shutting_down = True
        self.is_previewing = False
        self.is_capturing = False
        try:
            self.stop()
        except Exception:
            pass

        # Closing the websocket server lets start()/asyncio.run unwind
        # normally.  Avoid os._exit(), which can interrupt atomic rename or
        # manifest/state fsync in another task.


async def main():
    server = WebSocketServer()
    try:
        await server.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    asyncio.run(main())
