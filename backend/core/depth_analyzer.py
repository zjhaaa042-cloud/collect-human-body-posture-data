import numpy as np
from enum import Enum
from dataclasses import dataclass
from typing import Optional
from loguru import logger


class DistanceStatus(Enum):
    TOO_CLOSE = "too_close"
    OPTIMAL = "optimal"
    TOO_FAR = "too_far"
    NO_DATA = "no_data"


@dataclass
class DistanceInfo:
    distance_mm: float
    status: DistanceStatus
    confidence: float
    message: str
    distance_m: float = 0.0

    def __post_init__(self):
        self.distance_m = self.distance_mm / 1000.0


class DepthAnalyzer:
    def __init__(self, target_distance_mm: float = 1000, tolerance_mm: float = 200, roi_ratio: float = 0.3):
        self.target = target_distance_mm
        self.tolerance = tolerance_mm
        self.roi_ratio = roi_ratio
        self.history = []
        self.max_history = 5

    def analyze_distance(self, depth_frame: np.ndarray) -> DistanceInfo:
        try:
            if depth_frame is None or depth_frame.size == 0:
                return DistanceInfo(0, DistanceStatus.NO_DATA, 0, "无深度数据")

            h, w = depth_frame.shape
            roi_h = int(h * self.roi_ratio)
            roi_w = int(w * self.roi_ratio)
            y1 = (h - roi_h) // 2
            x1 = (w - roi_w) // 2
            roi = depth_frame[y1:y1 + roi_h, x1:x1 + roi_w]

            valid_values = roi[roi > 0]
            if len(valid_values) == 0:
                return DistanceInfo(0, DistanceStatus.NO_DATA, 0, "未检测到深度数据")

            median_dist = np.median(valid_values)
            self.history.append(median_dist)
            if len(self.history) > self.max_history:
                self.history.pop(0)
            avg_dist = np.mean(self.history)

            if avg_dist < self.target - self.tolerance:
                confidence = min(0.9, 1.0 - abs(avg_dist - (self.target - self.tolerance)) / self.tolerance)
                return DistanceInfo(
                    avg_dist,
                    DistanceStatus.TOO_CLOSE,
                    confidence,
                    f"太近了，请后退（{avg_dist / 1000:.1f}米）"
                )
            elif avg_dist > self.target + self.tolerance:
                confidence = min(0.9, 1.0 - abs(avg_dist - (self.target + self.tolerance)) / self.tolerance)
                return DistanceInfo(
                    avg_dist,
                    DistanceStatus.TOO_FAR,
                    confidence,
                    f"太远了，请靠近（{avg_dist / 1000:.1f}米）"
                )
            else:
                confidence = 0.95
                return DistanceInfo(
                    avg_dist,
                    DistanceStatus.OPTIMAL,
                    confidence,
                    f"距离合适（{avg_dist / 1000:.1f}米）"
                )
        except Exception as e:
            logger.error(f"Failed to analyze distance: {e}")
            return DistanceInfo(0, DistanceStatus.NO_DATA, 0, f"分析失败: {str(e)}")

    def get_distance_hint(self, info: DistanceInfo) -> str:
        hints = {
            DistanceStatus.TOO_CLOSE: "请向后退一步",
            DistanceStatus.OPTIMAL: "距离合适，请保持",
            DistanceStatus.TOO_FAR: "请向前走一步",
            DistanceStatus.NO_DATA: "请站在相机前方"
        }
        return hints.get(info.status, "")

    def reset(self):
        self.history.clear()
