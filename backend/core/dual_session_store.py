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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import cv2
import numpy as np

from .camera_adapters import FrameBundle
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
_LAYOUT_README = """双机八角度采集数据说明（dual-rgbd-v2.2）

每个受试者位于 subjects/<受试者编号>/。
session_manifest.json：受试者登记信息、八个角度进度、人体测量与完成状态。
angles/angle_000_front 等：一个角度一份目录；方向以 0 度正面为起点顺时针。
capture_01_<UTC时间>：一次双机近同步采集；内含两台相机各 5 帧 RGB、原始/对齐深度、两类伪彩深度和 PLY 点云。
capture_manifest.json：时间同步审计、每个文件的 SHA-256、距离及采集确认信息。

目录中的 depth_raw_uint16 与 depth_aligned_uint16 均为无损 uint16 PNG；depth_*_color 是便于查看的伪彩 PNG。
pointcloud_color_xyz_mm 是带 RGB 颜色的二进制 PLY，坐标单位为毫米，按每 4 像素取一个点以控制文件体积。
双机属于主机时钟近同步，不是硬件触发严格同步；请读取 capture_manifest.json 的 sync_audit 再进行融合分析。
"""


class DualSessionStoreError(RuntimeError):
    pass


class DualSessionStore:
    """Append-only dual-camera session storage with atomic group commits."""

    def __init__(self, output_root: str | Path) -> None:
        root = Path(output_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise DualSessionStoreError("输出路径不是目录")
        self.output_directory = root
        self.root = root / "body_posture_dual_v2"
        self.root.mkdir(parents=True, exist_ok=True)
        readme = self.root / "README_数据格式说明.txt"
        if not readme.exists():
            readme.write_text(_LAYOUT_README, encoding="utf-8")

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
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def create_session(
        self,
        subject_id: str,
        *,
        clothing_note: str = "",
        target_distance_mm: int | None = None,
    ) -> dict[str, Any]:
        subject_id = self._validate_subject_id(subject_id)
        subject_dir = self._subject_dir(subject_id)
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
            "completion": {
                "can_complete": False,
                "completed": False,
                "completed_at": None,
                "blockers": ["八个角度尚未全部采集", "必填人体测量尚未完成"],
            },
        }
        subject_dir.mkdir(parents=True)
        (subject_dir / ".staging").mkdir()
        self._atomic_json(self._state_path(subject_id), state)
        return state

    def get_session(self, subject_id: str) -> dict[str, Any]:
        subject_id = self._validate_subject_id(subject_id)
        path = self._state_path(subject_id)
        if not path.exists():
            raise DualSessionStoreError("未找到双机任务")
        state = self._read_json(path)
        # Keep unfinished v2.0/v2.1 sessions usable after upgrading the app.
        state.setdefault("status", "ACTIVE")
        state.setdefault("completed_at", None)
        state.setdefault("output_directory", str(Path(state.get("output_root", self.root)).parent))
        state.setdefault("anthropometry", {
            "status": "MISSING", "complete": False, "saved_at": None,
            "records": [], "missing_required": [],
        })
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

        state = self.get_session(subject_id)
        if state.get("status") == "COMPLETE":
            raise DualSessionStoreError("该受试者任务已完成，不能修改人体测量")
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

        state = self.get_session(subject_id)
        if state.get("status") == "COMPLETE":
            return state
        self._refresh_completion(state)
        blockers = list(state["completion"].get("blockers") or [])
        if blockers:
            raise DualSessionStoreError("；".join(blockers))
        subject_dir = self._subject_dir(subject_id)
        for group in state.get("angles", {}).values():
            attempts = group.get("attempts") or []
            if not attempts:
                raise DualSessionStoreError(f"{group.get('yaw_deg')}° 缺少已落盘采集记录")
            attempt_dir = subject_dir / attempts[-1]["path"]
            manifest = attempt_dir / (
                "capture_manifest.json" if state.get("layout_version") == "readable-v1" else "capture.json"
            )
            if not attempt_dir.is_dir() or not manifest.is_file():
                raise DualSessionStoreError(f"{group.get('yaw_deg')}° 采集目录或清单缺失")
            capture_manifest = self._read_json(manifest)
            for record in capture_manifest.get("files", []):
                path = attempt_dir / str(record.get("path") or "")
                expected_sha256 = str(record.get("sha256") or "")
                if not path.is_file():
                    raise DualSessionStoreError(
                        f"{group.get('yaw_deg')}° 文件缺失：{record.get('path')}"
                    )
                if not expected_sha256 or self._sha256(path) != expected_sha256:
                    raise DualSessionStoreError(
                        f"{group.get('yaw_deg')}° 文件完整性校验失败：{record.get('path')}"
                    )
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
            blockers.append("M01–M13 必填人体测量尚未完成")
        state["completion"] = {
            "can_complete": not blockers and state.get("status") != "COMPLETE",
            "completed": state.get("status") == "COMPLETE",
            "completed_at": state.get("completed_at"),
            "status": "COMPLETE" if state.get("status") == "COMPLETE" else "INCOMPLETE",
            "blockers": blockers,
        }

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
        state = self.get_session(subject_id)
        group = state["angles"][group_id]
        attempt_id = f"capture_{len(group['attempts']) + 1:02d}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
        subject_dir = self._subject_dir(subject_id)
        staging = subject_dir / ".staging" / attempt_id
        if state.get("layout_version") == "readable-v1":
            angle_dir = f"angle_{int(yaw_deg):03d}_{_ANGLE_NAMES[int(yaw_deg)]}"
            final_dir = subject_dir / "angles" / angle_dir / attempt_id
        else:
            # Preserve the original v2.0 paths for an unfinished older task.
            final_dir = subject_dir / "groups" / group_id / "attempts" / attempt_id
        staging.mkdir(parents=True)
        try:
            files = []
            for camera_code, frames in (("C336L", gemini_frames), ("CD435I", d435i_frames)):
                camera_directory = _CAMERAS[camera_code][0] if state.get("layout_version") == "readable-v1" else camera_code
                files.extend(self._write_camera_burst(
                    staging / camera_directory,
                    frames,
                    camera_code,
                    readable_layout=state.get("layout_version") == "readable-v1",
                ))
            capture = {
                "attempt_id": attempt_id,
                "subject_id": subject_id,
                "group_id": group_id,
                "yaw_deg": int(yaw_deg),
                "angle_name": _ANGLE_NAMES[int(yaw_deg)],
                "captured_at": self._now(),
                "cameras": ["C336L", "CD435I"],
                "metadata": dict(metadata),
                "sync_audit": dict(audit),
                "pointcloud": {
                    "format": "binary_little_endian_ply",
                    "coordinate_unit": "millimeter",
                    "color_order": "RGB",
                    "pixel_stride": _POINTCLOUD_STRIDE,
                },
                "files": files,
            }
            manifest_name = "capture_manifest.json" if state.get("layout_version") == "readable-v1" else "capture.json"
            self._atomic_json(staging / manifest_name, capture)
            capture["capture_manifest_sha256"] = self._sha256(staging / manifest_name)
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, final_dir)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        group["status"] = "CAPTURED"
        group["attempts"].append({
            "attempt_id": attempt_id,
            "path": str(final_dir.relative_to(subject_dir)).replace("\\", "/"),
            "captured_at": capture["captured_at"],
            "max_host_timestamp_skew_ms": audit.get("max_host_timestamp_skew_ms"),
        })
        self._refresh_completion(state)
        self._atomic_json(self._state_path(subject_id), state)
        return {"attempt_id": attempt_id, "group_id": group_id, "state": state, "capture": capture}

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
                    "sha256": self._sha256(path),
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
                "sha256": self._sha256(pointcloud_path),
            })
        return records

    @staticmethod
    def _write_png(path: Path, image: np.ndarray, *, is_rgb: bool) -> None:
        output = np.asarray(image)
        if is_rgb:
            output = cv2.cvtColor(output, cv2.COLOR_RGB2BGR)
        encoded_ok, encoded = cv2.imencode(".png", output)
        if not encoded_ok:
            raise DualSessionStoreError(f"无法写入 PNG：{path.name}")
        path.write_bytes(encoded.tobytes())

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
