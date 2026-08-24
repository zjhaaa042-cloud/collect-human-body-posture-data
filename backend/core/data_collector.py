import json
import base64
import re
import hashlib
import cv2
import numpy as np
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any
from loguru import logger

from ..storage.ply_writer import PLYWriter

SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]+$")
ANON_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


@dataclass
class CaptureConfig:
    save_rgb: bool = True
    save_depth: bool = True
    save_pointcloud: bool = True
    colored_pointcloud: bool = True
    pointcloud_binary: bool = True
    quality_check: bool = True
    min_depth_coverage: float = 0.3
    min_color_brightness: int = 30
    max_color_brightness: int = 220


@dataclass
class CaptureResult:
    session_id: str
    capture_id: str
    timestamp: str
    rgb_path: Optional[str] = None
    depth_path: Optional[str] = None
    depth_preview_path: Optional[str] = None
    pointcloud_path: Optional[str] = None
    pose_path: Optional[str] = None
    mask_path: Optional[str] = None
    calibration_path: Optional[str] = None
    bbox: Optional[List[int]] = None
    distance_mm: float = 0.0
    quality: Optional[dict] = None
    success: bool = True
    error: Optional[str] = None


class DataCollector:
    def __init__(self, output_dir: str):
        self.output_dir = Path(output_dir)
        self.sessions_dir = self.output_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.current_session = None
        self.session_id = None
        self.capture_count = 0
        self.session_metadata = None

    def _validate_session_name(self, session_name: str) -> str:
        session_name = (session_name or "").strip()
        if not session_name:
            raise ValueError("会话名称不能为空")
        if session_name in {".", ".."} or not SESSION_NAME_RE.fullmatch(session_name):
            raise ValueError("会话名称只能包含中文、英文、数字、下划线和短横线")
        return session_name

    @staticmethod
    def _write_image(path: Path, image: np.ndarray, params=None) -> bool:
        """Write OpenCV images through Python so Windows Unicode paths work."""
        encoded_ok, encoded = cv2.imencode(path.suffix or ".png", image, params or [])
        if not encoded_ok:
            return False
        path.write_bytes(encoded.tobytes())
        return True

    def _check_image_quality(self, color_data: np.ndarray, depth_data: np.ndarray, config: CaptureConfig) -> tuple:
        issues = []
        if color_data is not None:
            gray = cv2.cvtColor(color_data, cv2.COLOR_RGB2GRAY)
            mean_brightness = np.mean(gray)
            if mean_brightness < config.min_color_brightness:
                issues.append(f"图像过暗 (亮度: {mean_brightness:.0f})")
            elif mean_brightness > config.max_color_brightness:
                issues.append(f"图像过亮 (亮度: {mean_brightness:.0f})")
            blur_score = cv2.Laplacian(gray, cv2.CV_64F).var()
            if blur_score < 100:
                issues.append(f"图像模糊 (清晰度: {blur_score:.0f})")
        if depth_data is not None:
            total_pixels = depth_data.size
            valid_pixels = np.count_nonzero(depth_data)
            coverage = valid_pixels / total_pixels
            if coverage < config.min_depth_coverage:
                issues.append(f"深度数据不足 (覆盖率: {coverage:.1%})")
        is_ok = len(issues) == 0
        return is_ok, issues

    @staticmethod
    def _normalize_capture_context(context: Optional[dict]) -> dict:
        source = context or {}
        capture_group_id = str(source.get("capture_group_id") or "").strip()
        if not capture_group_id or len(capture_group_id) > 64 or not ANON_ID_RE.fullmatch(capture_group_id):
            raise ValueError("capture_group_id 必须为 1-64 位英文、数字、下划线或短横线")
        yaw = float(source.get("view_yaw_deg", 0))
        if yaw < -360 or yaw > 360:
            raise ValueError("view_yaw_deg 必须在 -360 到 360 之间")
        camera_height = source.get("camera_height_mm")
        camera_height = float(camera_height) if camera_height not in (None, "") else None
        if camera_height is not None and not 100 <= camera_height <= 3000:
            raise ValueError("camera_height_mm 必须在 100-3000 mm 之间")
        forced = bool(source.get("forced_capture"))
        quality_ready = bool(source.get("quality_ready"))
        return {
            "capture_group_id": capture_group_id,
            "view_yaw_deg": yaw,
            "pose_type": str(source.get("pose_type") or "standing_relaxed"),
            "clothing_type": str(source.get("clothing_type") or "unknown"),
            "camera_height_mm": camera_height,
            "forced_capture": forced,
            "qc_status": "needs_review" if forced or not quality_ready else "accepted",
            "manual_review": {
                "status": "pending" if forced or not quality_ready else "not_required",
                "reviewer_id": None,
                "reviewed_at": None,
                "notes": "",
            },
        }

    @staticmethod
    def _capture_configuration(snapshot: Dict[str, Any]) -> dict:
        camera = snapshot.get("camera") or {}
        if not snapshot.get("calibration_version"):
            raise ValueError("缺少 calibration_version")
        if not snapshot.get("orientation") or not snapshot.get("output_intrinsics"):
            raise ValueError("缺少当前帧方向或相机内参")
        if float(snapshot.get("depth_unit_mm") or 0) <= 0:
            raise ValueError("缺少有效的深度单位")
        if not (camera.get("serial_number") or camera.get("uid")):
            raise ValueError("缺少相机序列号或 UID")
        stable = {
            "calibration_version": snapshot.get("calibration_version"),
            "camera_serial_number": camera.get("serial_number") or camera.get("uid"),
            "orientation": snapshot.get("orientation"),
            "raw_resolution": snapshot.get("raw_resolution"),
            "output_resolution": snapshot.get("output_resolution"),
            "depth_unit_mm": snapshot.get("depth_unit_mm"),
            "alignment": snapshot.get("alignment"),
        }
        encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {**stable, "signature": hashlib.sha256(encoded.encode("utf-8")).hexdigest()}

    @staticmethod
    def _validate_anonymous_id(value: str, field_name: str) -> str:
        value = str(value or "").strip()
        if not value or len(value) > 64 or not ANON_ID_RE.fullmatch(value):
            raise ValueError(f"{field_name} 必须为 1-64 位英文、数字、下划线或短横线")
        return value

    @staticmethod
    def _normalize_measurement(measurement: Optional[dict]) -> dict:
        source = measurement or {}
        readings = source.get("raw_readings_cm") or []
        readings = [round(float(value), 2) for value in readings if value not in (None, "")]
        if any(value <= 0 or value > 300 for value in readings):
            raise ValueError("腰围原始测量值必须在 0-300 cm 之间")
        return {
            "target": "waist_cm",
            "anatomical_definition": str(source.get("anatomical_definition") or "最低肋骨与髂嵴中点高度的水平截面"),
            "protocol_version": str(source.get("protocol_version") or "waist_tape_v1"),
            "raw_readings_cm": readings,
            "mean_cm": round(float(np.mean(readings)), 2) if readings else None,
            "measurer_id": str(source.get("measurer_id") or "").strip(),
            "measured_at": source.get("measured_at"),
        }

    def create_session(self, session_name: Optional[str] = None, subject: Optional[dict] = None) -> str:
        try:
            subject = subject or {}
            subject_id = self._validate_anonymous_id(subject.get("subject_id"), "subject_id")
            visit_id = self._validate_anonymous_id(subject.get("visit_id") or "visit_001", "visit_id")
            if session_name is None:
                session_name = f"{subject_id}_{visit_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            else:
                session_name = self._validate_session_name(session_name)

            session_path = self.sessions_dir / session_name
            if session_path.resolve().parent != self.sessions_dir.resolve():
                raise ValueError("会话路径无效")
            if session_path.exists():
                raise FileExistsError(f"会话 {session_name} 已存在，请选择已有会话或使用新名称")
            self.session_id = session_name
            self.current_session = session_path
            self.current_session.mkdir(parents=True, exist_ok=True)

            (self.current_session / "rgb").mkdir(exist_ok=True)
            (self.current_session / "depth").mkdir(exist_ok=True)
            (self.current_session / "pointcloud").mkdir(exist_ok=True)
            (self.current_session / "pose").mkdir(exist_ok=True)
            (self.current_session / "mask").mkdir(exist_ok=True)
            (self.current_session / "calibration").mkdir(exist_ok=True)

            self.capture_count = 0
            self.session_metadata = {
                "format_version": 3,
                "session_id": session_name,
                "created_at": datetime.now().isoformat(),
                "status": "active",
                "subject": {
                    "subject_id": subject_id,
                    "visit_id": visit_id,
                },
                "measurement": self._normalize_measurement(subject.get("measurement")),
                "capture_configuration": None,
                "statistics": {
                    "total_captures": 0,
                    "successful_captures": 0,
                    "failed_captures": 0
                },
                "captures": []
            }

            self._save_metadata()
            logger.info(f"Session created: {session_name}")
            return session_name
        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            raise

    def capture(
        self,
        frame_data,
        point_cloud=None,
        config: Optional[CaptureConfig] = None,
        camera_intrinsics=None,
        quality_snapshot: Optional[dict] = None,
        pose_metadata: Optional[dict] = None,
        calibration_snapshot: Optional[dict] = None,
        body_mask: Optional[np.ndarray] = None,
        body_bbox: Optional[tuple] = None,
        mask_source: Optional[str] = None,
        capture_context: Optional[dict] = None,
    ) -> CaptureResult:
        try:
            if self.current_session is None or not self.current_session.exists():
                raise ValueError("请先创建包含匿名 subject_id 的采集会话")

            if int(self.session_metadata.get("format_version", 1)) < 3 and self.session_metadata.get("captures"):
                raise ValueError("旧格式会话仅供查看，请新建 v3 会话继续采集")
            if self.session_metadata.get("status") == "completed":
                raise ValueError("该会话已完成并锁定，请新建会话后再采集")

            if config is None:
                config = CaptureConfig()

            import shutil
            total, used, free = shutil.disk_usage(self.current_session)
            if free < 100 * 1024 * 1024:
                raise OSError("磁盘空间不足（剩余 < 100MB）")

            if config.quality_check:
                is_ok, issues = self._check_image_quality(frame_data.color, frame_data.depth, config)
                if not is_ok:
                    logger.warning(f"Quality check failed: {', '.join(issues)}")
                    return CaptureResult(
                        session_id=self.session_id or "",
                        capture_id=f"cap_{self.capture_count + 1:03d}",
                        timestamp=datetime.now().isoformat(),
                        success=False,
                        error=f"质量检查失败: {', '.join(issues)}"
                    )

            next_count = self.capture_count + 1
            capture_id = f"cap_{next_count:03d}"
            timestamp = int(datetime.now().timestamp() * 1000)
            timestamp_str = str(timestamp)

            capture_context = self._normalize_capture_context(capture_context)
            if capture_context["qc_status"] == "accepted" and body_mask is None:
                raise ValueError("合格采集必须包含人体 mask")
            if body_mask is not None and frame_data.depth is not None and body_mask.shape != frame_data.depth.shape:
                raise ValueError("人体 mask 与深度图分辨率不一致")
            calibration_snapshot = dict(calibration_snapshot or {})
            calibration_snapshot["captured_at"] = datetime.now().isoformat()
            configuration = self._capture_configuration(calibration_snapshot)
            existing_configuration = self.session_metadata.get("capture_configuration")
            if existing_configuration and configuration["signature"] != existing_configuration.get("signature"):
                raise ValueError("相机序列号、方向、分辨率、深度单位或标定版本已变化，请新建会话后再采集")

            result = CaptureResult(
                session_id=self.session_id,
                capture_id=capture_id,
                timestamp=datetime.now().isoformat(),
                distance_mm=float((quality_snapshot or {}).get("distance_mm", 0.0)),
                quality=quality_snapshot,
                bbox=list(body_bbox) if body_bbox else None,
            )

            if config.save_rgb and frame_data.color is not None:
                rgb_filename = f"rgb_{capture_id}_{timestamp_str}.png"
                rgb_path = self.current_session / "rgb" / rgb_filename
                if not self._write_image(
                    rgb_path,
                    cv2.cvtColor(frame_data.color, cv2.COLOR_RGB2BGR),
                    [cv2.IMWRITE_PNG_COMPRESSION, 0],
                ):
                    raise OSError(f"RGB 图像写入失败: {rgb_path}")
                result.rgb_path = f"rgb/{rgb_filename}"

            if config.save_depth and frame_data.depth is not None:
                depth_filename = f"depth_{capture_id}_{timestamp_str}.npz"
                depth_path = self.current_session / "depth" / depth_filename
                save_dict = {
                    "depth": frame_data.depth,
                    "depth_scale": frame_data.depth_scale,
                    "shape": np.array(frame_data.depth.shape)
                }
                if point_cloud is not None and point_cloud.pixel_indices is not None:
                    save_dict["pixel_indices"] = point_cloud.pixel_indices
                np.savez_compressed(str(depth_path), **save_dict)
                result.depth_path = f"depth/{depth_filename}"

                depth_png_filename = f"depth_{capture_id}_{timestamp_str}.png"
                depth_png_path = self.current_session / "depth" / depth_png_filename
                depth_mm = frame_data.depth.astype(np.float32) * frame_data.depth_scale
                valid_mask = depth_mm > 0
                if np.any(valid_mask):
                    depth_clipped = np.clip(depth_mm, 20, 5000)
                    depth_clipped = np.where(depth_mm > 20, depth_clipped, 0)
                    depth_norm = cv2.normalize(depth_clipped, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)
                    depth_norm[~valid_mask] = 0
                    depth_colored = cv2.applyColorMap(depth_norm, cv2.COLORMAP_JET)
                    depth_preview = depth_colored
                else:
                    depth_preview = frame_data.depth
                if not self._write_image(
                    depth_png_path,
                    depth_preview,
                    [cv2.IMWRITE_PNG_COMPRESSION, 0],
                ):
                    raise OSError(f"深度预览图写入失败: {depth_png_path}")
                result.depth_preview_path = f"depth/{depth_png_filename}"

            if config.save_pointcloud and point_cloud is not None:
                pc_filename = f"pc_{capture_id}_{timestamp_str}.ply"
                pc_path = self.current_session / "pointcloud" / pc_filename
                self._save_ply(point_cloud.points, point_cloud.colors, str(pc_path), binary=config.pointcloud_binary)
                result.pointcloud_path = f"pointcloud/{pc_filename}"

            if pose_metadata:
                (self.current_session / "pose").mkdir(exist_ok=True)
                pose_filename = f"pose_{capture_id}_{timestamp_str}.json"
                pose_path = self.current_session / "pose" / pose_filename
                with open(pose_path, "w", encoding="utf-8") as pose_file:
                    json.dump(pose_metadata, pose_file, ensure_ascii=False, indent=2)
                result.pose_path = f"pose/{pose_filename}"

            if body_mask is not None:
                mask_filename = f"mask_{capture_id}_{timestamp_str}.png"
                mask_path = self.current_session / "mask" / mask_filename
                mask_image = (body_mask > 0).astype(np.uint8) * 255
                if not self._write_image(
                    mask_path,
                    mask_image,
                    [cv2.IMWRITE_PNG_COMPRESSION, 3],
                ):
                    raise OSError("人体 mask 保存失败")
                result.mask_path = f"mask/{mask_filename}"

            calibration_filename = f"calibration_{capture_id}_{timestamp_str}.json"
            calibration_path = self.current_session / "calibration" / calibration_filename
            with open(calibration_path, "w", encoding="utf-8") as calibration_file:
                json.dump(calibration_snapshot, calibration_file, ensure_ascii=False, indent=2)
            result.calibration_path = f"calibration/{calibration_filename}"

            self.capture_count = next_count
            if not existing_configuration:
                self.session_metadata["capture_configuration"] = configuration
            self.session_metadata["statistics"]["total_captures"] += 1
            self.session_metadata["statistics"]["successful_captures"] += 1
            capture_record = asdict(result)
            capture_record.update(capture_context)
            capture_record["mask_source"] = mask_source
            capture_record["data_semantics"] = {
                "training_depth": "depth_path (raw uint16 NPZ)",
                "depth_preview": "visualization_only_per_frame_normalized",
                "pointcloud_coordinate_unit": "millimeter",
            }
            self.session_metadata["captures"].append(capture_record)
            self._save_metadata()

            logger.info(f"Capture successful: {capture_id}")
            return result
        except Exception as e:
            logger.error(f"Capture failed: {e}")
            if self.session_metadata:
                self.session_metadata["statistics"]["total_captures"] += 1
                self.session_metadata["statistics"]["failed_captures"] += 1
                self._save_metadata()
            return CaptureResult(
                session_id=self.session_id or "",
                capture_id=f"cap_{self.capture_count + 1:03d}",
                timestamp=datetime.now().isoformat(),
                success=False,
                error=str(e)
            )

    def _save_ply(self, points: np.ndarray, colors: Optional[np.ndarray], filepath: str, binary: bool = True):
        try:
            PLYWriter.save(filepath, points, colors, binary=binary)
        except Exception as e:
            logger.error(f"Failed to save PLY: {e}")
            raise

    def _save_metadata(self):
        try:
            if self.current_session and self.session_metadata:
                metadata_path = self.current_session / "metadata.json"
                with open(metadata_path, 'w', encoding='utf-8') as f:
                    json.dump(self.session_metadata, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save metadata: {e}")

    def close_session(self):
        try:
            if self.session_metadata:
                self.session_metadata["status"] = "completed"
                self.session_metadata["completed_at"] = datetime.now().isoformat()
                self._save_metadata()
            logger.info(f"Session closed: {self.session_id}")
        except Exception as e:
            logger.error(f"Failed to close session: {e}")

    def get_session_list(self) -> List[str]:
        try:
            if not self.sessions_dir.exists():
                return []
            return sorted(d.name for d in self.sessions_dir.iterdir() if d.is_dir())
        except Exception as e:
            logger.error(f"Failed to get session list: {e}")
            return []

    def select_session(self, session_name: str) -> bool:
        """Select an existing session to continue capturing"""
        try:
            session_name = self._validate_session_name(session_name)
            session_path = self.sessions_dir / session_name
            if not session_path.exists():
                logger.error(f"Session not found: {session_name}")
                return False
            
            self.session_id = session_name
            self.current_session = session_path
            
            # Load existing metadata
            metadata_path = session_path / "metadata.json"
            if metadata_path.exists():
                with open(metadata_path, 'r', encoding='utf-8') as f:
                    self.session_metadata = json.load(f)
                # Get existing capture count
                self.capture_count = self.session_metadata.get("statistics", {}).get("successful_captures", 0)
            else:
                # Create new metadata for existing session
                self.capture_count = 0
                self.session_metadata = {
                    "format_version": 1,
                    "session_id": session_name,
                    "created_at": datetime.now().isoformat(),
                    "status": "active",
                    "camera": {},
                    "statistics": {
                        "total_captures": 0,
                        "successful_captures": 0,
                        "failed_captures": 0
                    },
                    "captures": []
                }
                self._save_metadata()
            
            logger.info(f"Session selected: {session_name} (captures: {self.capture_count})")
            return True
        except Exception as e:
            logger.error(f"Failed to select session: {e}")
            return False

    def get_capture_count(self) -> int:
        return self.capture_count

    def update_capture_review(self, capture_id: str, status: str, reviewer_id: str = "", notes: str = "") -> dict:
        if not self.session_metadata:
            raise ValueError("当前没有可复核的会话")
        status = str(status or "").strip().lower()
        if status not in {"accepted", "isolated"}:
            raise ValueError("复核结果只能是 accepted 或 isolated")
        reviewer_id = str(reviewer_id or "").strip()
        if reviewer_id:
            reviewer_id = self._validate_anonymous_id(reviewer_id, "reviewer_id")
        notes = str(notes or "").strip()
        if len(notes) > 500:
            raise ValueError("复核备注不能超过 500 字")
        for item in self.session_metadata.get("captures", []):
            if item.get("capture_id") == capture_id:
                item["qc_status"] = status
                item["manual_review"] = {
                    "status": "completed",
                    "decision": status,
                    "reviewer_id": reviewer_id,
                    "reviewed_at": datetime.now().isoformat(),
                    "notes": notes,
                }
                self._save_metadata()
                return item
        raise FileNotFoundError(f"未找到采集记录: {capture_id}")

    def get_captures(self) -> list:
        try:
            if not self.current_session or not self.current_session.exists():
                return []
            if self.session_metadata and self.session_metadata.get("captures"):
                captures = []
                for i, item in enumerate(self.session_metadata.get("captures", []), 1):
                    rgb_path = item.get("rgb_path")
                    filename = Path(rgb_path).stem if rgb_path else item.get("capture_id", f"cap_{i:03d}")
                    timestamp = 0
                    if item.get("timestamp"):
                        try:
                            timestamp = datetime.fromisoformat(item["timestamp"]).timestamp()
                        except ValueError:
                            timestamp = 0
                    captures.append({
                        "index": i,
                        "capture_id": item.get("capture_id", f"cap_{i:03d}"),
                        "filename": filename,
                        "time": timestamp,
                        "has_image": bool(rgb_path),
                        "qc_status": item.get("qc_status", "legacy"),
                    })
                return captures
            rgb_dir = self.current_session / "rgb"
            if not rgb_dir.exists():
                return []
            files = sorted(rgb_dir.glob("*.png"), key=lambda f: f.stat().st_mtime)
            captures = []
            for i, f in enumerate(files, 1):
                name = f.stem
                captures.append({
                    "index": i,
                    "filename": name,
                    "time": f.stat().st_mtime
                })
            return captures
        except Exception as e:
            logger.error(f"Failed to get captures: {e}")
            return []

    def get_capture_image(self, filename: str) -> str:
        try:
            if not self.current_session or not self.current_session.exists():
                return ""
            rgb_path = self.current_session / "rgb" / f"{filename}.png"
            if not rgb_path.exists():
                return ""
            encoded = np.frombuffer(rgb_path.read_bytes(), dtype=np.uint8)
            img = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
            if img is None:
                return ""
            h, w = img.shape[:2]
            max_size = 640
            if max(h, w) > max_size:
                scale = max_size / max(h, w)
                img = cv2.resize(img, (int(w * scale), int(h * scale)))
            _, buffer = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 85])
            return base64.b64encode(buffer).decode('utf-8')
        except Exception as e:
            logger.error(f"Failed to get capture image: {e}")
            return ""
