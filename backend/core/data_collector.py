import json
import base64
import cv2
import numpy as np
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional, List
from loguru import logger


@dataclass
class CaptureConfig:
    save_rgb: bool = True
    save_depth: bool = True
    save_pointcloud: bool = True
    colored_pointcloud: bool = True
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
    pointcloud_path: Optional[str] = None
    distance_mm: float = 0.0
    success: bool = True
    error: Optional[str] = None


class DataCollector:
    def __init__(self, output_dir: str):
        self.output_dir = Path(output_dir)
        self.sessions_dir = self.output_dir / "sessions"
        self.current_session = None
        self.session_id = None
        self.capture_count = 0
        self.session_metadata = None

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

    def create_session(self, session_name: Optional[str] = None) -> str:
        try:
            if session_name is None:
                session_name = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

            self.session_id = session_name
            self.current_session = self.sessions_dir / session_name
            self.current_session.mkdir(parents=True, exist_ok=True)

            (self.current_session / "rgb").mkdir(exist_ok=True)
            (self.current_session / "depth").mkdir(exist_ok=True)
            (self.current_session / "pointcloud").mkdir(exist_ok=True)

            self.capture_count = 0
            self.session_metadata = {
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
            logger.info(f"Session created: {session_name}")
            return session_name
        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            raise

    def capture(self, frame_data, point_cloud=None, config: Optional[CaptureConfig] = None, camera_intrinsics=None) -> CaptureResult:
        try:
            if self.current_session is None:
                raise ValueError("No active session. Call create_session first.")

            if config is None:
                config = CaptureConfig()

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

            self.capture_count += 1
            capture_id = f"cap_{self.capture_count:03d}"
            timestamp = int(datetime.now().timestamp() * 1000)
            timestamp_str = str(timestamp)

            result = CaptureResult(
                session_id=self.session_id,
                capture_id=capture_id,
                timestamp=datetime.now().isoformat()
            )

            if config.save_rgb and frame_data.color is not None:
                rgb_filename = f"rgb_{capture_id}_{timestamp_str}.png"
                rgb_path = self.current_session / "rgb" / rgb_filename
                cv2.imwrite(str(rgb_path), cv2.cvtColor(frame_data.color, cv2.COLOR_RGB2BGR),
                           [cv2.IMWRITE_PNG_COMPRESSION, 0])
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
                cv2.imwrite(str(depth_png_path), frame_data.depth, [cv2.IMWRITE_PNG_COMPRESSION, 0])

            if config.save_pointcloud and point_cloud is not None:
                pc_filename = f"pc_{capture_id}_{timestamp_str}.ply"
                pc_path = self.current_session / "pointcloud" / pc_filename
                self._save_ply(point_cloud.points, point_cloud.colors, str(pc_path))
                result.pointcloud_path = f"pointcloud/{pc_filename}"

            if camera_intrinsics is not None:
                self.session_metadata["camera"] = {
                    "fx": camera_intrinsics.fx,
                    "fy": camera_intrinsics.fy,
                    "cx": camera_intrinsics.cx,
                    "cy": camera_intrinsics.cy,
                    "width": camera_intrinsics.width,
                    "height": camera_intrinsics.height
                }

            self.session_metadata["statistics"]["total_captures"] += 1
            self.session_metadata["statistics"]["successful_captures"] += 1
            self.session_metadata["captures"].append(asdict(result))
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
                capture_id=f"cap_{self.capture_count:03d}",
                timestamp=datetime.now().isoformat(),
                success=False,
                error=str(e)
            )

    def _save_ply(self, points: np.ndarray, colors: Optional[np.ndarray], filepath: str):
        try:
            valid_mask = np.any(np.abs(points) > 1e-6, axis=1)
            valid_points = points[valid_mask]
            valid_colors = colors[valid_mask] if colors is not None else None

            with open(filepath, 'w') as f:
                f.write("ply\n")
                f.write("format ascii 1.0\n")
                f.write(f"element vertex {len(valid_points)}\n")
                f.write("property float x\n")
                f.write("property float y\n")
                f.write("property float z\n")
                if valid_colors is not None:
                    f.write("property uchar red\n")
                    f.write("property uchar green\n")
                    f.write("property uchar blue\n")
                f.write("end_header\n")

                for i in range(len(valid_points)):
                    line = f"{valid_points[i, 0]:.3f} {valid_points[i, 1]:.3f} {valid_points[i, 2]:.3f}"
                    if valid_colors is not None:
                        line += f" {valid_colors[i, 0]} {valid_colors[i, 1]} {valid_colors[i, 2]}"
                    f.write(line + "\n")
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
            return [d.name for d in self.sessions_dir.iterdir() if d.is_dir()]
        except Exception as e:
            logger.error(f"Failed to get session list: {e}")
            return []

    def select_session(self, session_name: str) -> bool:
        """Select an existing session to continue capturing"""
        try:
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
                self.capture_count = self.session_metadata.get("statistics", {}).get("total_captures", 0)
            else:
                # Create new metadata for existing session
                self.capture_count = 0
                self.session_metadata = {
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

    def get_captures(self) -> list:
        try:
            if not self.current_session or not self.current_session.exists():
                return []
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
            img = cv2.imread(str(rgb_path))
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
