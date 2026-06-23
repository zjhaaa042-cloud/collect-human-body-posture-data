import cv2
import numpy as np
import base64
from typing import Tuple, Optional
from loguru import logger

MIN_DEPTH_MM = 20
MAX_DEPTH_MM = 5000


class FrameProcessor:
    def __init__(self, preview_size: Tuple[int, int] = (320, 240), jpeg_quality: int = 50):
        self.preview_size = preview_size
        self.jpeg_quality = jpeg_quality
        self._encode_params = [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]

    def encode_preview(self, frame: np.ndarray, is_rgb: bool = True) -> str:
        try:
            if frame is None or frame.size == 0:
                return ""
            h, w = frame.shape[:2]
            target_w, target_h = self.preview_size
            if w != target_w or h != target_h:
                resized = cv2.resize(frame, self.preview_size, interpolation=cv2.INTER_LINEAR)
            else:
                resized = frame
            if is_rgb:
                resized = cv2.cvtColor(resized, cv2.COLOR_RGB2BGR)
            _, buffer = cv2.imencode('.jpg', resized, self._encode_params)
            return base64.b64encode(buffer).decode('utf-8')
        except Exception as e:
            logger.error(f"Failed to encode preview: {e}")
            return ""

    def encode_preview_fast(self, frame: np.ndarray, is_rgb: bool = True) -> str:
        try:
            if frame is None or frame.size == 0:
                return ""
            h, w = frame.shape[:2]
            target_w, target_h = self.preview_size
            if w != target_w or h != target_h:
                resized = cv2.resize(frame, self.preview_size, interpolation=cv2.INTER_NEAREST)
            else:
                resized = frame
            if is_rgb:
                resized = cv2.cvtColor(resized, cv2.COLOR_RGB2BGR)
            _, buffer = cv2.imencode('.jpg', resized, self._encode_params)
            return base64.b64encode(buffer).decode('utf-8')
        except Exception as e:
            logger.error(f"Failed to encode preview fast: {e}")
            return ""

    def encode_depth_preview_fast(self, depth: np.ndarray, depth_scale: float = 1.0) -> str:
        try:
            if depth is None or depth.size == 0:
                return ""
            h, w = depth.shape[:2]
            target_w, target_h = self.preview_size
            if w != target_w or h != target_h:
                small_depth = cv2.resize(depth, self.preview_size, interpolation=cv2.INTER_NEAREST)
            else:
                small_depth = depth
            valid_mask = small_depth > 0
            if not np.any(valid_mask):
                return ""
            depth_mm = small_depth.astype(np.float32) * depth_scale
            depth_clipped = np.clip(depth_mm, MIN_DEPTH_MM, MAX_DEPTH_MM)
            depth_clipped = np.where(depth_mm > MIN_DEPTH_MM, depth_clipped, 0)
            depth_norm = cv2.normalize(depth_clipped, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)
            depth_norm[~valid_mask] = 0
            colored = cv2.applyColorMap(depth_norm, cv2.COLORMAP_JET)
            _, buffer = cv2.imencode('.jpg', colored, self._encode_params)
            return base64.b64encode(buffer).decode('utf-8')
        except Exception as e:
            logger.error(f"Failed to encode depth preview fast: {e}")
            return ""

    def depth_to_colorized(self, depth: np.ndarray) -> np.ndarray:
        try:
            if depth is None or depth.size == 0:
                return np.zeros((self.preview_size[1], self.preview_size[0], 3), dtype=np.uint8)
            valid_mask = depth > 0
            if not np.any(valid_mask):
                return np.zeros((depth.shape[0], depth.shape[1], 3), dtype=np.uint8)
            depth_norm = cv2.normalize(depth.astype(np.float32), None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)
            depth_norm[~valid_mask] = 0
            return cv2.applyColorMap(depth_norm, cv2.COLORMAP_JET)
        except Exception as e:
            logger.error(f"Failed to colorize depth: {e}")
            return np.zeros((self.preview_size[1], self.preview_size[0], 3), dtype=np.uint8)

    def create_distance_indicator(self, distance_mm: float, status: str, size: Tuple[int, int] = (200, 50)) -> np.ndarray:
        try:
            indicator = np.zeros((size[1], size[0], 3), dtype=np.uint8)

            if status == "optimal":
                color = (0, 200, 83)
            elif status == "too_close":
                color = (0, 0, 255)
            elif status == "too_far":
                color = (0, 200, 255)
            else:
                color = (128, 128, 128)

            cv2.rectangle(indicator, (0, 0), (size[0]-1, size[1]-1), color, 2)

            text = f"{distance_mm/1000:.1f}m"
            font = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = 0.7
            thickness = 2
            text_size = cv2.getTextSize(text, font, font_scale, thickness)[0]
            text_x = (size[0] - text_size[0]) // 2
            text_y = (size[1] + text_size[1]) // 2
            cv2.putText(indicator, text, (text_x, text_y), font, font_scale, color, thickness)

            return indicator
        except Exception as e:
            logger.error(f"Failed to create distance indicator: {e}")
            return np.zeros((size[1], size[0], 3), dtype=np.uint8)

    def combine_frames(self, color: np.ndarray, depth_colorized: np.ndarray,
                      orientation: str = "horizontal") -> np.ndarray:
        try:
            if orientation == "horizontal":
                return np.hstack((color, depth_colorized))
            else:
                return np.vstack((color, depth_colorized))
        except Exception as e:
            logger.error(f"Failed to combine frames: {e}")
            return color

    def add_overlay_text(self, frame: np.ndarray, text: str,
                        position: Tuple[int, int] = (10, 30),
                        color: Tuple[int, int, int] = (255, 255, 255)) -> np.ndarray:
        try:
            result = frame.copy()
            font = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = 0.7
            thickness = 2

            cv2.putText(result, text, position, font, font_scale, (0, 0, 0), thickness + 2)
            cv2.putText(result, text, position, font, font_scale, color, thickness)

            return result
        except Exception as e:
            logger.error(f"Failed to add overlay text: {e}")
            return frame
