"""Storage for the flexible dual-camera eight-angle protocol.

The format is intentionally separate from RealAnthro-RGBD-v1: one angle is a
single group containing two independently calibrated camera bursts.  This
avoids changing historical one-camera attempts while preserving pair-level
time evidence for the new protocol.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import cv2
import numpy as np

from .camera_adapters import FrameBundle
from ..storage.atomic_io import (
    AtomicIOError,
    DatasetLease,
    atomic_write_json,
    atomic_write_npy,
    replace_with_retry,
    safe_join,
    sha256_file,
)
from ..storage.ply_writer import PLYWriter


DUAL_ANGLES = tuple(range(0, 360, 45))
FRAME_COUNT = 5
_ANGLE_NAMES = {
    0: "front", 45: "front_right", 90: "right", 135: "back_right",
    180: "back", 225: "back_left", 270: "left", 315: "front_left",
}
_CAMERAS = {
    "C336L": ("camera_gemini_336l", "Gemini 336L"),
    "CD435I": ("camera_realsense_d435i", "Intel RealSense D435i"),
}
_MODALITIES = {
    "rgb": ("rgb_color", "RGB 彩色图"),
    "depth_raw": ("depth_raw_uint16", "原始深度（uint16 PNG）"),
    "depth_aligned": ("depth_aligned_uint16", "对齐深度（uint16 PNG）"),
}
_POINTCLOUD_STRIDE = 4
_POINTCLOUD_MAX_DEPTH_MM = 6000.0
_DISK_RESERVE_BYTES = 512 * 1024 * 1024
_STORAGE_FEATURES = (
    "depth_npy_uint16_v1",
    "per_frame_calibration_v1",
    "durable_commit_v1",
)
_LAYOUT_README = """双机八角度采集数据说明（dual-rgbd-v2.2）

每个受试者位于 subjects/<受试者编号>/。
session_manifest.json：受试者登记信息、八个角度进度、人体测量与完成状态。
angles/angle_000_front 等：一个角度一份目录；方向以 0 度正面为起点顺时针。
capture_01_<UTC时间>：一次双机近同步采集；内含两台相机各 5 帧 RGB、原始/对齐深度、两类伪彩深度和 PLY 点云。
capture_manifest.json：时间同步审计、每个文件的 SHA-256、距离及采集确认信息。

目录中的 depth_raw_uint16 与 depth_aligned_uint16 均为无损 uint16 PNG；depth_raw_npy 与 depth_aligned_npy 是逐像素一致的原始 uint16 NumPy 数组。
NPY 不预乘深度比例；毫米值 = NPY 数值 × capture_manifest.json 中对应帧的 depth_scale_mm_per_unit。
depth_*_color 是便于查看的伪彩 PNG。
pointcloud_color_xyz_mm 是带 RGB 颜色的二进制 PLY，坐标单位为毫米，按每 4 像素取一个点以控制文件体积。
双机属于主机时钟近同步，不是硬件触发严格同步；请读取 capture_manifest.json 的 sync_audit 再进行融合分析。
"""


class DualSessionStoreError(RuntimeError):
    pass


class DualSessionStore:
    """Append-only dual-camera session storage with atomic group commits."""

    _locks_guard = threading.Lock()
    _subject_locks: dict[tuple[str, str], threading.RLock] = {}

    def __init__(self, output_root: str | Path) -> None:
        root = Path(output_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise DualSessionStoreError("输出路径不是目录")
        self.output_directory = root
        self.root = root / "body_posture_dual_v2"
        self.root.mkdir(parents=True, exist_ok=True)
        lease_name = hashlib.sha256(
            os.path.normcase(str(self.root)).encode("utf-8")
        ).hexdigest()
        try:
            self._lease = DatasetLease(
                Path(tempfile.gettempdir())
                / "body_posture_collector_locks"
                / f"dual_{lease_name}.lock",
                error_message="已有采集实例持有该双机数据集锁，拒绝并发写入",
            )
        except AtomicIOError as exc:
            raise DualSessionStoreError(str(exc)) from exc
        readme = self.root / "README_数据格式说明.txt"
        if not readme.exists():
            readme.write_text(_LAYOUT_README, encoding="utf-8")

    def close(self) -> None:
        lease = getattr(self, "_lease", None)
        if lease is not None:
            lease.release()
            self._lease = None

    def __enter__(self) -> "DualSessionStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - interpreter timing varies.
        try:
            self.close()
        except Exception:
            pass

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _validate_subject_id(subject_id: str) -> str:
        value = str(subject_id or "").strip().upper()
        if not value or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in value):
            raise DualSessionStoreError("受试者编号只能包含字母、数字、下划线或连字符")
        return value

    def _subject_dir(self, subject_id: str) -> Path:
        return self.root / "subjects" / subject_id

    def _state_path(self, subject_id: str) -> Path:
        subject_dir = self._subject_dir(subject_id)
        legacy = subject_dir / "session.json"
        return legacy if legacy.exists() else subject_dir / "session_manifest.json"

    @staticmethod
    def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
        atomic_write_json(path, value)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _sha256(path: Path) -> str:
        return sha256_file(path)

    def _lock_for(self, subject_id: str) -> threading.RLock:
        key = (os.path.normcase(str(self.root)), subject_id)
        with self._locks_guard:
            return self._subject_locks.setdefault(key, threading.RLock())

    @staticmethod
    def _jsonable(value: Any) -> Any:
        if is_dataclass(value):
            return DualSessionStore._jsonable(asdict(value))
        if isinstance(value, Mapping):
            return {str(key): DualSessionStore._jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [DualSessionStore._jsonable(item) for item in value]
        if isinstance(value, np.generic):
            return value.item()
        return value

    @staticmethod
    def _frame_metadata(frame: FrameBundle, frame_index: int) -> dict[str, Any]:
        return {
            "frame_index": frame_index,
            "host_timestamp_ns": int(frame.host_timestamp_ns),
            "device_timestamp": frame.device_timestamp,
            "frame_number": frame.frame_number,
            "depth_scale_mm_per_unit": float(frame.depth_scale),
            "stream_timestamps": DualSessionStore._jsonable(frame.stream_timestamps),
            "stream_frame_numbers": DualSessionStore._jsonable(frame.stream_frame_numbers),
            "intrinsics": DualSessionStore._jsonable(frame.intrinsics),
            "extrinsics": DualSessionStore._jsonable(frame.extrinsics),
            "camera_metadata": DualSessionStore._jsonable(frame.camera_metadata),
        }

    def create_session(
        self,
        subject_id: str,
        *,
        clothing_note: str = "",
        target_distance_mm: int | None = None,
    ) -> dict[str, Any]:
        subject_id = self._validate_subject_id(subject_id)
        subject_dir = self._subject_dir(subject_id)
        with self._lock_for(subject_id):
            if subject_dir.exists():
                raise DualSessionStoreError("该受试者双机任务已存在，请选择新的编号")
            note = str(clothing_note or "").strip()
            if len(note) > 500:
                raise DualSessionStoreError("服装备注不能超过 500 字")
            if target_distance_mm is not None and not 250 <= int(target_distance_mm) <= 6000:
                raise DualSessionStoreError("自定义距离必须在 250–6000 mm")
            state = {
                "schema_version": "dual-rgbd-v2.2",
                "layout_version": "readable-v1",
                "storage_features": list(_STORAGE_FEATURES),
                "subject_id": subject_id,
                "status": "ACTIVE",
                "created_at": self._now(),
                "completed_at": None,
                "output_directory": str(self.output_directory),
                "output_root": str(self.root),
                "clothing_note": note,
                "target_distance_mm": int(target_distance_mm) if target_distance_mm else None,
                "angles": {
                    f"V{angle:03d}": {"yaw_deg": angle, "status": "PENDING", "attempts": []}
                    for angle in DUAL_ANGLES
                },
                "anthropometry": {
                    "status": "MISSING",
                    "complete": False,
                    "saved_at": None,
                    "records": [],
                    "missing_required": [],
                },
                "integrity": {"status": "OK", "errors": [], "checked_at": self._now()},
                "reconciliation_required": False,
                "recovery_report": None,
                "completion": {
                    "can_complete": False,
                    "completed": False,
                    "completed_at": None,
                    "blockers": ["八个角度尚未全部采集", "必填人体测量尚未完成"],
                },
            }
            subject_dir.mkdir(parents=True)
            try:
                (subject_dir / ".staging").mkdir()
                self._atomic_json(self._state_path(subject_id), state)
            except Exception:
                shutil.rmtree(subject_dir, ignore_errors=True)
                raise
            return state

    def get_session(self, subject_id: str) -> dict[str, Any]:
        subject_id = self._validate_subject_id(subject_id)
        with self._lock_for(subject_id):
            self.reconcile_session(subject_id)
            return self._read_state(subject_id)

    def _read_state(self, subject_id: str) -> dict[str, Any]:
        path = self._state_path(subject_id)
        if not path.exists():
            raise DualSessionStoreError("未找到双机任务")
        state = self._read_json(path)
        # Keep unfinished v2.0/v2.1/v2.2 sessions usable after upgrading.
        state.setdefault("status", "ACTIVE")
        state.setdefault("completed_at", None)
        state.setdefault("storage_features", [])
        state.setdefault("output_directory", str(Path(state.get("output_root", self.root)).parent))
        state.setdefault("anthropometry", {
            "status": "MISSING", "complete": False, "saved_at": None,
            "records": [], "missing_required": [],
        })
        state.setdefault("integrity", {"status": "UNKNOWN", "errors": [], "checked_at": None})
        state.setdefault("reconciliation_required", False)
        state.setdefault("recovery_report", None)
        state.setdefault("completion", {
            "can_complete": False, "completed": False, "completed_at": None,
            "blockers": [],
        })
        return state

    def save_anthropometry(
        self,
        subject_id: str,
        records: Sequence[Mapping[str, Any]],
        definitions: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Validate and atomically save the measurements for a dual session."""

        subject_id = self._validate_subject_id(subject_id)
        with self._lock_for(subject_id):
            state = self.get_session(subject_id)
            self._assert_writable(state)
            return self._save_anthropometry_locked(
                subject_id, records, definitions, state
            )

    def _save_anthropometry_locked(
        self,
        subject_id: str,
        records: Sequence[Mapping[str, Any]],
        definitions: Sequence[Mapping[str, Any]],
        state: dict[str, Any],
    ) -> dict[str, Any]:

        definition_map = {
            str(item.get("measurement_id") or "").upper(): dict(item)
            for item in definitions if isinstance(item, Mapping)
        }
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for record in records:
            if not isinstance(record, Mapping):
                raise DualSessionStoreError("每条人体测量记录必须是对象")
            measurement_id = str(record.get("measurement_id") or "").upper()
            definition = definition_map.get(measurement_id)
            if definition is None:
                raise DualSessionStoreError(f"未知人体测量项目：{measurement_id}")
            field_name = str(record.get("field_name") or "")
            if field_name not in {str(item) for item in definition.get("field_names", [])}:
                raise DualSessionStoreError(f"{measurement_id} 不包含字段 {field_name}")
            key = (measurement_id, field_name)
            if key in seen:
                raise DualSessionStoreError(f"人体测量字段重复：{measurement_id}/{field_name}")
            seen.add(key)
            try:
                m1 = float(record.get("m1"))
                m2 = float(record.get("m2"))
                m3_raw = record.get("m3")
                m3 = float(m3_raw) if m3_raw not in {None, ""} else None
            except (TypeError, ValueError) as exc:
                raise DualSessionStoreError(f"{measurement_id}/{field_name} 的读数必须是数字") from exc
            if m1 <= 0 or m2 <= 0 or (m3 is not None and m3 <= 0):
                raise DualSessionStoreError(f"{measurement_id}/{field_name} 的读数必须大于 0")
            threshold = definition.get("third_measurement_threshold")
            if threshold is not None and abs(m1 - m2) > float(threshold) and m3 is None:
                raise DualSessionStoreError(f"{measurement_id}/{field_name} 前两次差值超限，必须填写第三次")
            item = {
                "measurement_id": measurement_id,
                "field_name": field_name,
                "m1": m1,
                "m2": m2,
            }
            if m3 is not None:
                item["m3"] = m3
            normalized.append(item)

        missing_required = []
        for measurement_id, definition in definition_map.items():
            if not bool(definition.get("required")):
                continue
            for field_name in map(str, definition.get("field_names", [])):
                if (measurement_id, field_name) not in seen:
                    missing_required.append(f"{measurement_id}/{field_name}")
        if missing_required:
            raise DualSessionStoreError(
                f"仍有 {len(missing_required)} 个必填人体测量字段未填写"
            )

        saved_at = self._now()
        state["anthropometry"] = {
            "status": "COMPLETE",
            "complete": True,
            "saved_at": saved_at,
            "records": normalized,
            "missing_required": [],
        }
        self._refresh_completion(state)
        self._atomic_json(self._state_path(subject_id), state)
        return state

    def complete_session(self, subject_id: str) -> dict[str, Any]:
        """Close a dual session after all eight angles and measurements exist."""

        subject_id = self._validate_subject_id(subject_id)
        with self._lock_for(subject_id):
            state = self.get_session(subject_id)
            if state.get("status") == "COMPLETE":
                return state
            self._assert_writable(state)
            self._refresh_completion(state)
            blockers = list(state["completion"].get("blockers") or [])
            if blockers:
                raise DualSessionStoreError("；".join(blockers))
            subject_dir = self._subject_dir(subject_id)
            for group in state.get("angles", {}).values():
                attempts = group.get("attempts") or []
                if not attempts:
                    raise DualSessionStoreError(f"{group.get('yaw_deg')}° 缺少已落盘采集记录")
                try:
                    attempt_dir = safe_join(subject_dir, attempts[-1]["path"])
                except AtomicIOError as exc:
                    raise DualSessionStoreError(str(exc)) from exc
                self._verify_attempt_directory(attempt_dir, require_commit=False)
            completed_at = self._now()
            state["status"] = "COMPLETE"
            state["completed_at"] = completed_at
            state["completion"] = {
                "can_complete": False,
                "completed": True,
                "completed_at": completed_at,
                "status": "COMPLETE",
                "blockers": [],
            }
            self._atomic_json(self._state_path(subject_id), state)
            return state

    @staticmethod
    def _refresh_completion(state: dict[str, Any]) -> None:
        angles = list(state.get("angles", {}).values())
        captured = sum(item.get("status") == "CAPTURED" for item in angles)
        blockers = []
        if captured < len(DUAL_ANGLES):
            blockers.append(f"双机八角度尚未完成（{captured}/{len(DUAL_ANGLES)}）")
        if state.get("anthropometry", {}).get("complete") is not True:
            blockers.append("5 项必填人体测量尚未完成")
        state["completion"] = {
            "can_complete": not blockers and state.get("status") != "COMPLETE",
            "completed": state.get("status") == "COMPLETE",
            "completed_at": state.get("completed_at"),
            "status": "COMPLETE" if state.get("status") == "COMPLETE" else "INCOMPLETE",
            "blockers": blockers,
        }

    @staticmethod
    def _assert_writable(state: Mapping[str, Any]) -> None:
        if str(state.get("status") or "").upper() == "COMPLETE":
            raise DualSessionStoreError("该受试者任务已完成并锁定，不能继续写入")
        if state.get("reconciliation_required") is True:
            raise DualSessionStoreError("该任务存在待恢复或完整性异常，修复前禁止继续写入")
        integrity = state.get("integrity") or {}
        if str(integrity.get("status") or "").upper() == "ERROR":
            raise DualSessionStoreError("该任务完整性检查失败，禁止继续写入")

    def _final_attempt_dir(
        self,
        state: Mapping[str, Any],
        subject_id: str,
        yaw_deg: int,
        attempt_id: str,
    ) -> Path:
        subject_dir = self._subject_dir(subject_id)
        if state.get("layout_version") == "readable-v1":
            angle_dir = f"angle_{yaw_deg:03d}_{_ANGLE_NAMES[yaw_deg]}"
            return subject_dir / "angles" / angle_dir / attempt_id
        return subject_dir / "groups" / f"V{yaw_deg:03d}" / "attempts" / attempt_id

    @staticmethod
    def _attempt_state_record(
        capture: Mapping[str, Any], final_dir: Path, subject_dir: Path
    ) -> dict[str, Any]:
        return {
            "attempt_id": str(capture["attempt_id"]),
            "path": final_dir.relative_to(subject_dir).as_posix(),
            "captured_at": capture.get("captured_at"),
            "capture_manifest_sha256": capture.get("capture_manifest_sha256"),
            "max_host_timestamp_skew_ms": (
                capture.get("sync_audit") or {}
            ).get("max_host_timestamp_skew_ms"),
        }

    @staticmethod
    def _estimate_group_bytes(*bursts: Sequence[FrameBundle]) -> int:
        estimate = 0
        for frames in bursts:
            for frame in frames:
                color = np.asarray(frame.color) if frame.color is not None else None
                raw = np.asarray(frame.depth_raw) if frame.depth_raw is not None else None
                aligned = (
                    np.asarray(frame.depth_aligned)
                    if frame.depth_aligned is not None
                    else None
                )
                # Conservative allowance for PNG + NPY + depth previews.
                if color is not None:
                    estimate += color.nbytes * 2
                for depth in (raw, aligned):
                    if depth is not None:
                        estimate += depth.nbytes * 3
                if aligned is not None:
                    point_count = max(1, aligned.size // (_POINTCLOUD_STRIDE ** 2))
                    estimate += point_count * 18
        return int(estimate * 1.25)

    def _assert_disk_capacity(
        self,
        subject_dir: Path,
        gemini_frames: Sequence[FrameBundle],
        d435i_frames: Sequence[FrameBundle],
    ) -> None:
        required = self._estimate_group_bytes(gemini_frames, d435i_frames)
        free = shutil.disk_usage(subject_dir).free
        if free < required + _DISK_RESERVE_BYTES:
            required_mb = (required + _DISK_RESERVE_BYTES) / (1024 * 1024)
            free_mb = free / (1024 * 1024)
            raise DualSessionStoreError(
                f"磁盘空间不足：本组写入及安全余量约需 {required_mb:.0f} MiB，"
                f"当前仅剩 {free_mb:.0f} MiB"
            )

    def _attempt_directories(self, subject_dir: Path) -> list[Path]:
        candidates = list((subject_dir / "angles").glob("angle_*/capture_*"))
        candidates.extend((subject_dir / "groups").glob("V*/attempts/capture_*"))
        return sorted(path for path in candidates if path.is_dir())

    def _verify_attempt_directory(
        self, directory: Path, *, require_commit: bool
    ) -> dict[str, Any]:
        manifest_path = directory / "capture_manifest.json"
        if not manifest_path.is_file():
            legacy = directory / "capture.json"
            manifest_path = legacy if legacy.is_file() else manifest_path
        if not manifest_path.is_file():
            raise DualSessionStoreError("采集目录缺少 capture_manifest.json")
        capture = self._read_json(manifest_path)
        attempt_id = str(capture.get("attempt_id") or "")
        subject_id = str(capture.get("subject_id") or "")
        try:
            yaw_deg = int(capture.get("yaw_deg"))
        except (TypeError, ValueError) as exc:
            raise DualSessionStoreError("采集清单角度无效") from exc
        if attempt_id != directory.name or not subject_id or yaw_deg not in DUAL_ANGLES:
            raise DualSessionStoreError("采集目录与清单身份不一致")
        files = capture.get("files")
        if not isinstance(files, list) or not files:
            raise DualSessionStoreError("采集清单没有文件记录")

        commit_path = directory / "commit.json"
        durable_commit_expected = "durable_commit_v1" in set(
            capture.get("storage_features") or []
        )
        if (require_commit or durable_commit_expected) and not commit_path.is_file():
            raise DualSessionStoreError("staging 尚无耐久 commit.json")
        if commit_path.is_file():
            commit = self._read_json(commit_path)
            manifest_record = commit.get("capture_manifest") or {}
            if (
                commit.get("status") != "COMMITTED"
                or commit.get("attempt_id") != attempt_id
                or commit.get("subject_id") != subject_id
                or int(commit.get("yaw_deg", -1)) != yaw_deg
                or int(commit.get("file_count", -1)) != len(files)
                or commit.get("files") != files
                or manifest_record.get("path") != manifest_path.name
                or manifest_record.get("sha256") != self._sha256(manifest_path)
            ):
                raise DualSessionStoreError("commit.json 与采集清单不一致")

        npy_records: dict[tuple[str, str, str], tuple[Mapping[str, Any], np.ndarray]] = {}
        png_records: dict[tuple[str, str, str], tuple[Mapping[str, Any], Path]] = {}
        for record in files:
            if not isinstance(record, Mapping):
                raise DualSessionStoreError("文件清单记录格式无效")
            try:
                path = safe_join(directory, str(record.get("path") or ""))
            except AtomicIOError as exc:
                raise DualSessionStoreError(str(exc)) from exc
            if not path.is_file():
                raise DualSessionStoreError(f"文件缺失：{record.get('path')}")
            if "size_bytes" in record and path.stat().st_size != int(record["size_bytes"]):
                raise DualSessionStoreError(f"文件大小不一致：{record.get('path')}")
            if self._sha256(path) != str(record.get("sha256") or ""):
                raise DualSessionStoreError(f"文件哈希不一致：{record.get('path')}")
            camera_code = str(record.get("camera_code") or "")
            frame_code = str(record.get("frame") or "")
            modality = str(record.get("modality") or "")
            if modality in {"depth_raw_npy", "depth_aligned_npy"}:
                array = np.load(path, allow_pickle=False)
                expected_shape = tuple(record.get("shape") or ())
                if (
                    array.dtype != np.uint16
                    or array.ndim != 2
                    or not array.flags.c_contiguous
                    or tuple(array.shape) != expected_shape
                    or float(record.get("depth_scale_mm_per_unit") or 0) <= 0
                ):
                    raise DualSessionStoreError(f"NPY 数组元数据无效：{record.get('path')}")
                logical = str(record.get("logical_modality") or modality.removesuffix("_npy"))
                npy_records[(camera_code, frame_code, logical)] = (record, array)
            elif modality in {"depth_raw", "depth_aligned"}:
                png_records[(camera_code, frame_code, modality)] = (record, path)

        if "depth_npy_uint16_v1" in set(capture.get("storage_features") or []):
            expected_npy_count = len(_CAMERAS) * FRAME_COUNT * 2
            if len(npy_records) != expected_npy_count:
                raise DualSessionStoreError(
                    f"NPY 深度文件数量不完整：{len(npy_records)}/{expected_npy_count}"
                )
            for key, (_, array) in npy_records.items():
                png_entry = png_records.get(key)
                if png_entry is None:
                    raise DualSessionStoreError(f"NPY 缺少对应 uint16 PNG：{key}")
                encoded = np.frombuffer(png_entry[1].read_bytes(), dtype=np.uint8)
                decoded = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
                if decoded is None or not np.array_equal(decoded, array):
                    raise DualSessionStoreError(f"NPY 与 PNG 深度值不一致：{key}")

        capture["capture_manifest_sha256"] = self._sha256(manifest_path)
        return capture

    def reconcile_session(self, subject_id: str) -> dict[str, Any]:
        """Recover durable orphan attempts without deleting partial evidence."""

        subject_id = self._validate_subject_id(subject_id)
        with self._lock_for(subject_id):
            state = self._read_state(subject_id)
            subject_dir = self._subject_dir(subject_id)
            known = {
                str(attempt.get("attempt_id") or "")
                for group in state.get("angles", {}).values()
                for attempt in group.get("attempts", [])
            }
            staging_root = subject_dir / ".staging"
            staging_root.mkdir(parents=True, exist_ok=True)
            staging_dirs = sorted(path for path in staging_root.iterdir() if path.is_dir())
            final_dirs = self._attempt_directories(subject_dir)
            orphan_finals = [path for path in final_dirs if path.name not in known]
            if (
                not staging_dirs
                and not orphan_finals
                and str((state.get("integrity") or {}).get("status") or "") == "OK"
            ):
                return dict(state.get("recovery_report") or {
                    "recovered_attempts": 0, "promoted_staging": 0, "errors": []
                })

            recovered = 0
            promoted = 0
            errors: list[str] = []
            candidates: list[tuple[Path, dict[str, Any]]] = []
            for staging in staging_dirs:
                try:
                    # Pre-upgrade v2.2 staging has no commit.json. It is still
                    # recoverable when its complete manifest and every file hash
                    # verify. New durable_commit_v1 staging always requires commit.
                    capture = self._verify_attempt_directory(staging, require_commit=False)
                    final_dir = self._final_attempt_dir(
                        state, subject_id, int(capture["yaw_deg"]), str(capture["attempt_id"])
                    )
                    if final_dir.exists():
                        raise DualSessionStoreError("staging 与 final 同时存在，拒绝自动覆盖")
                    if str(state.get("status") or "").upper() == "COMPLETE":
                        raise DualSessionStoreError("完成任务中发现额外 staging")
                    final_dir.parent.mkdir(parents=True, exist_ok=True)
                    replace_with_retry(staging, final_dir, allow_existing_destination=False)
                    promoted += 1
                    candidates.append((final_dir, capture))
                except Exception as exc:
                    errors.append(f"{staging.relative_to(subject_dir).as_posix()}: {exc}")

            for final_dir in orphan_finals:
                try:
                    capture = self._verify_attempt_directory(final_dir, require_commit=False)
                    if str(state.get("status") or "").upper() == "COMPLETE":
                        raise DualSessionStoreError("完成任务中发现未登记的额外采集")
                    candidates.append((final_dir, capture))
                except Exception as exc:
                    errors.append(f"{final_dir.relative_to(subject_dir).as_posix()}: {exc}")

            for final_dir, capture in sorted(
                candidates,
                key=lambda item: (str(item[1].get("captured_at") or ""), item[0].name),
            ):
                attempt_id = str(capture["attempt_id"])
                if attempt_id in known:
                    continue
                if str(capture.get("subject_id") or "") != subject_id:
                    errors.append(f"{attempt_id}: subject_id 不一致")
                    continue
                group_id = f"V{int(capture['yaw_deg']):03d}"
                group = state.get("angles", {}).get(group_id)
                if not isinstance(group, dict):
                    errors.append(f"{attempt_id}: 角度不属于当前任务")
                    continue
                group["attempts"].append(
                    self._attempt_state_record(capture, final_dir, subject_dir)
                )
                group["attempts"].sort(
                    key=lambda item: (str(item.get("captured_at") or ""), item["attempt_id"])
                )
                group["status"] = "CAPTURED"
                known.add(attempt_id)
                recovered += 1

            report = {
                "checked_at": self._now(),
                "recovered_attempts": recovered,
                "promoted_staging": promoted,
                "errors": errors,
            }
            state["recovery_report"] = report
            state["reconciliation_required"] = bool(errors)
            state["integrity"] = {
                "status": "ERROR" if errors else "OK",
                "errors": errors,
                "checked_at": report["checked_at"],
            }
            self._refresh_completion(state)
            self._atomic_json(self._state_path(subject_id), state)
            return report

    def commit_group(
        self,
        subject_id: str,
        yaw_deg: int,
        gemini_frames: Sequence[FrameBundle],
        d435i_frames: Sequence[FrameBundle],
        *,
        audit: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        subject_id = self._validate_subject_id(subject_id)
        group_id = f"V{int(yaw_deg):03d}"
        if int(yaw_deg) not in DUAL_ANGLES:
            raise DualSessionStoreError("角度必须为 0–315 之间的 45°步进")
        if len(gemini_frames) != FRAME_COUNT or len(d435i_frames) != FRAME_COUNT:
            raise DualSessionStoreError("两台相机均必须提供 5 帧")
        with self._lock_for(subject_id):
            self.reconcile_session(subject_id)
            state = self._read_state(subject_id)
            self._assert_writable(state)
            group = state["angles"][group_id]
            subject_dir = self._subject_dir(subject_id)
            attempt_number = len(group["attempts"]) + 1
            attempt_id = (
                f"capture_{attempt_number:02d}_"
                f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}"
            )
            staging = subject_dir / ".staging" / attempt_id
            final_dir = self._final_attempt_dir(state, subject_id, int(yaw_deg), attempt_id)
            self._assert_disk_capacity(subject_dir, gemini_frames, d435i_frames)
            staging.mkdir(parents=True, exist_ok=False)
            manifest_name = (
                "capture_manifest.json"
                if state.get("layout_version") == "readable-v1"
                else "capture.json"
            )
            try:
                files = []
                frame_metadata: dict[str, list[dict[str, Any]]] = {}
                for camera_code, frames in (
                    ("C336L", gemini_frames),
                    ("CD435I", d435i_frames),
                ):
                    camera_directory = (
                        _CAMERAS[camera_code][0]
                        if state.get("layout_version") == "readable-v1"
                        else camera_code
                    )
                    files.extend(self._write_camera_burst(
                        staging / camera_directory,
                        frames,
                        camera_code,
                        readable_layout=state.get("layout_version") == "readable-v1",
                    ))
                    frame_metadata[camera_code] = [
                        self._frame_metadata(frame, index)
                        for index, frame in enumerate(frames, 1)
                    ]
                capture = {
                    "schema_version": "dual-capture-v1.1",
                    "storage_features": list(_STORAGE_FEATURES),
                    "attempt_id": attempt_id,
                    "subject_id": subject_id,
                    "group_id": group_id,
                    "yaw_deg": int(yaw_deg),
                    "angle_name": _ANGLE_NAMES[int(yaw_deg)],
                    "captured_at": self._now(),
                    "cameras": ["C336L", "CD435I"],
                    "frames": frame_metadata,
                    "metadata": self._jsonable(dict(metadata)),
                    "sync_audit": self._jsonable(dict(audit)),
                    "pointcloud": {
                        "format": "binary_little_endian_ply",
                        "coordinate_unit": "millimeter",
                        "color_order": "RGB",
                        "pixel_stride": _POINTCLOUD_STRIDE,
                    },
                    "files": files,
                }
                manifest_path = staging / manifest_name
                self._atomic_json(manifest_path, capture)
                manifest_sha256 = self._sha256(manifest_path)
                commit = {
                    "schema_version": "dual-commit-v1.0",
                    "status": "COMMITTED",
                    "attempt_id": attempt_id,
                    "subject_id": subject_id,
                    "group_id": group_id,
                    "yaw_deg": int(yaw_deg),
                    "committed_at": self._now(),
                    "capture_manifest": {
                        "path": manifest_name,
                        "sha256": manifest_sha256,
                    },
                    "file_count": len(files),
                    "files": files,
                }
                self._atomic_json(staging / "commit.json", commit)
                self._verify_attempt_directory(staging, require_commit=True)
                final_dir.parent.mkdir(parents=True, exist_ok=True)
                replace_with_retry(staging, final_dir, allow_existing_destination=False)
            except Exception as exc:
                # Preserve staging evidence. Recovery will only promote a complete,
                # hash-verified commit and will block writes for partial evidence.
                raise DualSessionStoreError(f"双机采集写入失败，staging 已保留：{exc}") from exc

            capture["capture_manifest_sha256"] = manifest_sha256
            group["status"] = "CAPTURED"
            group["attempts"].append(
                self._attempt_state_record(capture, final_dir, subject_dir)
            )
            state["storage_features"] = list(_STORAGE_FEATURES)
            state["integrity"] = {"status": "OK", "errors": [], "checked_at": self._now()}
            state["reconciliation_required"] = False
            self._refresh_completion(state)
            try:
                self._atomic_json(self._state_path(subject_id), state)
            except Exception as exc:
                raise DualSessionStoreError(
                    "数据已完整落盘，但状态账本待自动恢复；请重新打开任务"
                ) from exc
            return {
                "attempt_id": attempt_id,
                "group_id": group_id,
                "state": state,
                "capture": capture,
            }

    def _write_camera_burst(
        self,
        directory: Path,
        frames: Sequence[FrameBundle],
        camera_code: str,
        *,
        readable_layout: bool,
    ) -> list[dict[str, Any]]:
        records = []
        for index, frame in enumerate(frames, 1):
            modalities = {
                "rgb": (frame.color, True),
                "depth_raw": frame.depth_raw,
                "depth_aligned": frame.depth_aligned,
            }
            for modality, source in modalities.items():
                image, is_rgb = source if isinstance(source, tuple) else (source, False)
                if image is None:
                    raise DualSessionStoreError(f"{camera_code} F{index:02d} 缺少 {modality}")
                modality_directory, modality_name = _MODALITIES[modality] if readable_layout else (modality, modality)
                filename = f"frame_{index:02d}.png" if readable_layout else f"F{index:02d}.png"
                path = directory / modality_directory / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                self._write_png(path, image, is_rgb=is_rgb)
                records.append({
                    "camera_code": camera_code,
                    "camera_name": _CAMERAS[camera_code][1],
                    "frame": f"frame_{index:02d}",
                    "modality": modality,
                    "modality_name": modality_name,
                    "path": str(path.relative_to(directory.parent)).replace("\\", "/"),
                    "format": "png",
                    "dtype": str(np.asarray(image).dtype),
                    "shape": list(np.asarray(image).shape),
                    "size_bytes": path.stat().st_size,
                    "sha256": self._sha256(path),
                    **(
                        {
                            "array_channel_order": "RGB",
                            "file_color_space": "sRGB",
                            "png_decoder_channel_order": "implementation_defined",
                        }
                        if modality == "rgb"
                        else {}
                    ),
                })
                if modality in {"depth_raw", "depth_aligned"}:
                    npy_path = directory / f"{modality}_npy" / (
                        f"frame_{index:02d}.npy" if readable_layout else f"F{index:02d}.npy"
                    )
                    npy_integrity = atomic_write_npy(npy_path, np.asarray(image))
                    records.append({
                        "camera_code": camera_code,
                        "camera_name": _CAMERAS[camera_code][1],
                        "frame": f"frame_{index:02d}",
                        "modality": f"{modality}_npy",
                        "logical_modality": modality,
                        "modality_name": f"{modality_name} NumPy 原始数组",
                        "path": str(npy_path.relative_to(directory.parent)).replace("\\", "/"),
                        **npy_integrity,
                        "depth_scale_mm_per_unit": float(frame.depth_scale),
                        "value_semantics": "sensor_depth_units",
                    })
            for depth_key in ("depth_raw", "depth_aligned"):
                depth = getattr(frame, depth_key)
                colorized_path = directory / (
                    f"{depth_key}_color" if readable_layout else f"{depth_key}_color"
                ) / (f"frame_{index:02d}.png" if readable_layout else f"F{index:02d}.png")
                colorized_path.parent.mkdir(parents=True, exist_ok=True)
                self._write_png(
                    colorized_path,
                    self._colorize_depth(depth, frame.depth_scale),
                    is_rgb=False,
                )
                records.append({
                    "camera_code": camera_code,
                    "camera_name": _CAMERAS[camera_code][1],
                    "frame": f"frame_{index:02d}",
                    "modality": f"{depth_key}_color",
                    "modality_name": f"{_MODALITIES[depth_key][1]}伪彩预览",
                    "path": str(colorized_path.relative_to(directory.parent)).replace("\\", "/"),
                    "format": "png",
                    "dtype": "uint8",
                    "shape": list(depth.shape) + [3],
                    "size_bytes": colorized_path.stat().st_size,
                    "sha256": self._sha256(colorized_path),
                })
            pointcloud_path = directory / "pointcloud_color_xyz_mm" / (
                f"frame_{index:02d}.ply" if readable_layout else f"F{index:02d}.ply"
            )
            pointcloud_path.parent.mkdir(parents=True, exist_ok=True)
            points, colors = self._build_colored_pointcloud(frame, camera_code)
            PLYWriter.save(str(pointcloud_path), points, colors, binary=True)
            records.append({
                "camera_code": camera_code,
                "camera_name": _CAMERAS[camera_code][1],
                "frame": f"frame_{index:02d}",
                "modality": "pointcloud_color_xyz_mm",
                "modality_name": "带 RGB 颜色的点云（毫米坐标）",
                "path": str(pointcloud_path.relative_to(directory.parent)).replace("\\", "/"),
                "format": "ply",
                "size_bytes": pointcloud_path.stat().st_size,
                "sha256": self._sha256(pointcloud_path),
            })
        return records

    @staticmethod
    def _write_png(path: Path, image: np.ndarray, *, is_rgb: bool) -> None:
        source = np.asarray(image)
        output = source
        if is_rgb:
            if source.dtype != np.uint8 or source.ndim != 3 or source.shape[2] != 3:
                raise DualSessionStoreError("RGB 必须为 uint8 HxWx3")
            output = cv2.cvtColor(output, cv2.COLOR_RGB2BGR)
        encoded_ok, encoded = cv2.imencode(".png", output)
        if not encoded_ok:
            raise DualSessionStoreError(f"无法写入 PNG：{path.name}")
        path.write_bytes(encoded.tobytes())
        decoded = cv2.imdecode(
            np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_UNCHANGED
        )
        if decoded is None or not np.array_equal(decoded, output):
            raise DualSessionStoreError(f"PNG 无损回读校验失败：{path.name}")

    @staticmethod
    def _colorize_depth(depth: np.ndarray, depth_scale: float) -> np.ndarray:
        depth_mm = np.asarray(depth, dtype=np.float32) * float(depth_scale)
        valid = (depth_mm > 0) & (depth_mm <= _POINTCLOUD_MAX_DEPTH_MM)
        normalized = np.clip(depth_mm / _POINTCLOUD_MAX_DEPTH_MM, 0.0, 1.0)
        colorized = cv2.applyColorMap(((1.0 - normalized) * 255.0).astype(np.uint8), cv2.COLORMAP_TURBO)
        colorized[~valid] = 0
        return colorized

    @staticmethod
    def _build_colored_pointcloud(frame: FrameBundle, camera_code: str) -> tuple[np.ndarray, np.ndarray]:
        depth = frame.depth_aligned
        color = frame.color
        intrinsics = frame.intrinsics.get("depth_aligned") or frame.intrinsics.get("color")
        if depth is None or color is None or intrinsics is None:
            raise DualSessionStoreError(f"{camera_code} 缺少生成彩色点云所需的对齐深度、RGB 或内参")
        if depth.shape[:2] != color.shape[:2]:
            raise DualSessionStoreError(f"{camera_code} 对齐深度与 RGB 尺寸不一致，拒绝生成错误点云")
        fx = float(getattr(intrinsics, "fx", 0.0))
        fy = float(getattr(intrinsics, "fy", 0.0))
        if fx <= 0 or fy <= 0:
            raise DualSessionStoreError(f"{camera_code} 点云内参无效")
        cy = float(getattr(intrinsics, "cy", depth.shape[0] / 2.0))
        cx = float(getattr(intrinsics, "cx", depth.shape[1] / 2.0))
        sampled_depth = depth[::_POINTCLOUD_STRIDE, ::_POINTCLOUD_STRIDE].astype(np.float32)
        sampled_color = color[::_POINTCLOUD_STRIDE, ::_POINTCLOUD_STRIDE]
        rows, columns = np.mgrid[0:depth.shape[0]:_POINTCLOUD_STRIDE, 0:depth.shape[1]:_POINTCLOUD_STRIDE]
        z = sampled_depth * float(frame.depth_scale)
        valid = np.isfinite(z) & (z > 0) & (z <= _POINTCLOUD_MAX_DEPTH_MM)
        points = np.column_stack((
            ((columns[valid] - cx) * z[valid] / fx),
            ((rows[valid] - cy) * z[valid] / fy),
            z[valid],
        )).astype(np.float32)
        if not len(points):
            raise DualSessionStoreError(f"{camera_code} 没有可用深度点，无法生成点云")
        return points, np.asarray(sampled_color[valid], dtype=np.uint8)
