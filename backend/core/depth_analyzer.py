from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

import cv2
import numpy as np
from loguru import logger

from .pose_analyzer import PoseAnalyzer

HUMAN_DEPTH_MIN_MM = 300
HUMAN_DEPTH_MAX_MM = 5000


class DistanceStatus(Enum):
    TOO_CLOSE = "too_close"
    OPTIMAL = "optimal"
    TOO_FAR = "too_far"
    NO_DATA = "no_data"
    NO_HUMAN = "no_human"
    BODY_INCOMPLETE = "body_incomplete"
    QUALITY_LOW = "quality_low"
    UNSTABLE = "unstable"


@dataclass
class DistanceInfo:
    distance_mm: float
    status: DistanceStatus
    confidence: float
    message: str
    distance_m: float = 0.0
    full_body_visible: bool = False
    visibility_score: float = 0.0
    score: float = 0.0
    ready: bool = False
    edge_margin: float = 0.0
    body_depth_coverage: float = 0.0
    stable: bool = False
    reasons: List[str] = field(default_factory=list)
    recommended_action: str = ""
    pose_available: bool = False
    pose_detected: bool = False
    landmarks_2d: List[dict] = field(default_factory=list)

    def __post_init__(self):
        self.distance_m = self.distance_mm / 1000.0

    def to_capture_quality(self) -> dict:
        return {
            "score": round(float(self.score), 1),
            "ready": bool(self.ready),
            "distance_mm": round(float(self.distance_mm), 1),
            "full_body_visible": bool(self.full_body_visible),
            "edge_margin": round(float(self.edge_margin), 4),
            "body_depth_coverage": round(float(self.body_depth_coverage), 4),
            "stable": bool(self.stable),
            "reasons": list(self.reasons),
            "recommended_action": self.recommended_action,
            "pose_available": bool(self.pose_available),
            "pose_detected": bool(self.pose_detected),
        }


class DepthAnalyzer:
    """Pose-aware body completeness, depth and capture readiness analyzer."""

    def __init__(
        self,
        target_distance_mm: float = 1500,
        tolerance_mm: float = 300,
        roi_ratio: float = 0.3,
        min_distance_mm: float = 1300,
        max_distance_mm: float = 2000,
        min_edge_margin: float = 0.04,
        min_body_depth_coverage: float = 0.75,
        min_quality_score: float = 80.0,
        pose_model_path: str = "models/pose_landmarker_full.task",
    ):
        self.target = float(target_distance_mm)
        self.tolerance = float(tolerance_mm)
        self.roi_ratio = roi_ratio
        self.min_distance = float(min_distance_mm)
        self.max_distance = float(max_distance_mm)
        self.min_edge_margin = float(min_edge_margin)
        self.min_body_depth_coverage = float(min_body_depth_coverage)
        self.min_quality_score = float(min_quality_score)
        self.pose_analyzer = PoseAnalyzer(pose_model_path)
        self.history = []
        self.landmark_history = []
        self.max_history = 10
        self._last_mask = None
        self._last_bbox = None
        self._last_mask_source = None

    @staticmethod
    def _bbox_from_mask(mask: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
        ys, xs = np.where(mask > 0)
        if xs.size < 30:
            return None
        return int(xs.min()), int(ys.min()), int(xs.max() + 1), int(ys.max() + 1)

    @staticmethod
    def _fallback_depth_body(depth_mm: np.ndarray):
        """Diagnostic fallback only; it never authorizes automatic collection."""
        source_h, source_w = depth_mm.shape
        scale = min(1.0, 320.0 / source_w)
        work = cv2.resize(depth_mm, (int(source_w * scale), int(source_h * scale)), interpolation=cv2.INTER_NEAREST) if scale < 1 else depth_mm
        valid = (work >= HUMAN_DEPTH_MIN_MM) & (work <= HUMAN_DEPTH_MAX_MM)
        if np.count_nonzero(valid) < work.size * 0.015:
            return None, None
        values = work[valid]
        candidates = []
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        for center in range(int(np.percentile(values, 2) // 150 * 150), int(np.percentile(values, 98)) + 150, 150):
            layer = (valid & (np.abs(work - center) <= 360)).astype(np.uint8)
            layer = cv2.morphologyEx(layer, cv2.MORPH_OPEN, kernel)
            layer = cv2.morphologyEx(layer, cv2.MORPH_CLOSE, kernel, iterations=2)
            count, labels, stats, _ = cv2.connectedComponentsWithStats(layer, 8)
            for label in range(1, count):
                x, y, w, h, area = stats[label]
                area_ratio = area / work.size
                aspect = h / max(w, 1)
                if 0.015 <= area_ratio <= 0.60 and h / work.shape[0] >= 0.25 and 0.6 <= aspect <= 5.5:
                    centrality = abs((x + w / 2) - work.shape[1] / 2) / max(work.shape[1] / 2, 1)
                    candidates.append((2 * h / work.shape[0] + area_ratio - 0.3 * centrality, labels == label))
        if not candidates:
            return None, None
        mask = max(candidates, key=lambda item: item[0])[1].astype(np.uint8)
        if scale < 1:
            mask = cv2.resize(mask, (source_w, source_h), interpolation=cv2.INTER_NEAREST)
        return mask, DepthAnalyzer._bbox_from_mask(mask)

    @staticmethod
    def _pose_mask(pose_result, shape):
        if not pose_result or not pose_result.detected or pose_result.segmentation_mask is None:
            return None
        mask = pose_result.segmentation_mask
        if mask.shape != shape:
            mask = cv2.resize(mask, (shape[1], shape[0]), interpolation=cv2.INTER_LINEAR)
        binary = (mask >= 0.45).astype(np.uint8)
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        return binary if np.count_nonzero(binary) >= 30 else None

    @staticmethod
    def _edge_margin(bbox, shape) -> float:
        if bbox is None:
            return 0.0
        h, w = shape
        x1, y1, x2, y2 = bbox
        return max(0.0, min(x1 / w, y1 / h, (w - x2) / w, (h - y2) / h))

    @staticmethod
    def _landmarks_complete(landmarks: List[dict], margin: float = 0.025) -> bool:
        if len(landmarks) < 33:
            return False
        groups = ((0, 1, 2, 3, 4), (11, 12), (23, 24), (25, 26), (27, 28, 29, 30, 31, 32))
        for group in groups:
            reliable = [landmarks[i] for i in group if landmarks[i].get("presence", 0) >= 0.45]
            if not reliable:
                return False
            if not any(margin <= item["x"] <= 1 - margin and margin <= item["y"] <= 1 - margin for item in reliable):
                return False
        for index in (15, 16, 27, 28, 31, 32):
            item = landmarks[index]
            if item.get("presence", 0) >= 0.55 and not (margin <= item["x"] <= 1 - margin and margin <= item["y"] <= 1 - margin):
                return False
        return True

    @staticmethod
    def _rgb_quality(color_frame: np.ndarray):
        if color_frame is None or color_frame.size == 0:
            return 0.0, 0.0, False
        gray = cv2.cvtColor(color_frame, cv2.COLOR_RGB2GRAY)
        brightness = float(np.mean(gray))
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        return brightness, sharpness, 30 <= brightness <= 220 and sharpness >= 80

    def _stability(self, distance_mm: float, landmarks: List[dict]):
        self.history.append(float(distance_mm))
        self.history = self.history[-self.max_history:]
        stable_indices = (0, 11, 12, 23, 24, 25, 26, 27, 28)
        vector = np.array([[landmarks[i]["x"], landmarks[i]["y"]] for i in stable_indices], dtype=np.float32) if len(landmarks) >= 33 else None
        if vector is not None:
            self.landmark_history.append(vector)
            self.landmark_history = self.landmark_history[-self.max_history:]
        if len(self.history) < 5:
            return False
        distance_stable = max(self.history[-5:]) - min(self.history[-5:]) <= 30.0
        landmark_stable = False
        if len(self.landmark_history) >= 5:
            diffs = [np.mean(np.linalg.norm(self.landmark_history[i] - self.landmark_history[i - 1], axis=1)) for i in range(-4, 0)]
            landmark_stable = float(np.mean(diffs)) <= 0.015
        return distance_stable and landmark_stable

    def analyze_distance(
        self,
        depth_frame: np.ndarray,
        color_frame: np.ndarray = None,
        depth_scale: float = 1.0,
        wait_for_pose: bool = False,
    ) -> DistanceInfo:
        try:
            if depth_frame is None or depth_frame.size == 0:
                return DistanceInfo(0, DistanceStatus.NO_DATA, 0, "无深度数据", reasons=["无深度数据"])
            depth_mm = depth_frame.astype(np.float32) * depth_scale
            submitted_timestamp = None
            if color_frame is not None:
                submitted_timestamp = self.pose_analyzer.submit(color_frame, force=wait_for_pose)
            pose = (
                self.pose_analyzer.wait_for_result(submitted_timestamp)
                if wait_for_pose and submitted_timestamp is not None
                else self.pose_analyzer.get_latest()
            )
            pose_available = self.pose_analyzer.available
            landmarks = pose.landmarks if pose and pose.detected else []
            mask = self._pose_mask(pose, depth_frame.shape)
            mask_source = "mediapipe" if mask is not None else None
            bbox = self._bbox_from_mask(mask) if mask is not None else None
            if mask is None:
                mask, bbox = self._fallback_depth_body(depth_mm)
                if mask is not None:
                    mask_source = "depth_fallback"
            if mask is None:
                self.history.clear()
                self.landmark_history.clear()
                reason = "正在等待姿态识别" if pose_available else "未识别到人体；姿态模型不可用"
                return DistanceInfo(0, DistanceStatus.NO_HUMAN, 0, reason, reasons=[reason], recommended_action="请站到相机正前方", pose_available=pose_available)

            self._last_mask, self._last_bbox = mask, bbox
            self._last_mask_source = mask_source
            valid_depth = (depth_mm >= HUMAN_DEPTH_MIN_MM) & (depth_mm <= HUMAN_DEPTH_MAX_MM)
            mask_count = max(1, int(np.count_nonzero(mask)))
            coverage = np.count_nonzero((mask > 0) & valid_depth) / mask_count
            values = depth_mm[(mask > 0) & valid_depth]
            if values.size < 30:
                return DistanceInfo(0, DistanceStatus.NO_DATA, 0, "人体区域没有有效深度", body_depth_coverage=coverage, reasons=["人体深度缺失"])
            near_surface = float(np.percentile(values, 40))
            torso_values = values[np.abs(values - near_surface) <= max(250.0, near_surface * 0.18)]
            distance = float(np.median(torso_values if torso_values.size else values))
            edge_margin = self._edge_margin(bbox, depth_frame.shape)
            landmarks_ok = self._landmarks_complete(landmarks)
            full_body = bool(pose_available and pose and pose.detected and landmarks_ok and edge_margin >= self.min_edge_margin)
            stable = self._stability(distance, landmarks)
            if color_frame is not None and bbox:
                x1, y1, x2, y2 = bbox
                rgb_region = color_frame[y1:y2, x1:x2]
            else:
                rgb_region = color_frame
            brightness, sharpness, rgb_ok = self._rgb_quality(rgb_region)
            distance_ok = self.min_distance <= distance <= self.max_distance
            adaptive_distance_ok = distance_ok and (distance <= self.target + 100.0 or edge_margin <= 0.08)

            reasons = []
            if not pose_available:
                reasons.append("MediaPipe姿态模型不可用")
            elif not pose or not pose.detected:
                reasons.append("未稳定识别人体关键点")
            if edge_margin < self.min_edge_margin:
                reasons.append("人体或肢体距离画面边缘过近")
            if not landmarks_ok:
                reasons.append("头部、躯干或四肢末端未完整识别")
            if coverage < self.min_body_depth_coverage:
                reasons.append("人体区域有效深度不足")
            if distance < self.min_distance:
                reasons.append("距离过近")
            elif distance > self.max_distance:
                reasons.append("距离过远")
            elif not adaptive_distance_ok:
                reasons.append("画面余量充足，可以继续靠近以提高数据质量")
            if not rgb_ok:
                reasons.append("RGB亮度或清晰度不足")
            if not stable:
                reasons.append("姿态尚未稳定")

            margin_score = min(1.0, edge_margin / max(self.min_edge_margin * 2, 0.001)) * 15
            coverage_score = min(1.0, coverage / max(self.min_body_depth_coverage, 0.01)) * 20
            completeness_score = 25 if full_body else (10 if bbox is not None else 0)
            if distance < self.min_distance or distance > self.max_distance:
                distance_score = 0
            else:
                distance_score = max(4.0, 10.0 - max(0.0, distance - self.target) / 100.0)
            rgb_score = 10 if rgb_ok else 4
            stability_score = 20 if stable else 0
            score = min(100.0, completeness_score + margin_score + coverage_score + distance_score + rgb_score + stability_score)
            ready = bool(full_body and coverage >= self.min_body_depth_coverage and adaptive_distance_ok and rgb_ok and stable and score >= self.min_quality_score)

            if edge_margin < self.min_edge_margin or not landmarks_ok:
                action = "请向后移动约10厘米，确保头、手和脚完整入镜"
                status = DistanceStatus.BODY_INCOMPLETE
            elif distance < self.min_distance:
                action, status = "请向后移动约10厘米", DistanceStatus.TOO_CLOSE
            elif distance > self.max_distance:
                action, status = "画面余量允许，请向前移动约10厘米", DistanceStatus.TOO_FAR
            elif not adaptive_distance_ok:
                action, status = "画面余量充足，请向前移动约10厘米以提高数据质量", DistanceStatus.TOO_FAR
            elif coverage < self.min_body_depth_coverage or not rgb_ok:
                action, status = "请调整站位、光线或衣物，改善人体深度与清晰度", DistanceStatus.QUALITY_LOW
            elif not stable:
                action, status = "请保持当前姿态不动", DistanceStatus.UNSTABLE
            else:
                action, status = "采集条件已满足", DistanceStatus.OPTIMAL
            message = f"质量 {score:.0f}分，人体距离 {distance / 1000:.2f}米；{action}"
            confidence = min(0.98, 0.5 + coverage * 0.35 + (0.1 if pose and pose.detected else 0))
            return DistanceInfo(
                distance, status, confidence, message,
                full_body_visible=full_body,
                visibility_score=min(1.0, edge_margin / max(self.min_edge_margin, 0.001)),
                score=score, ready=ready, edge_margin=edge_margin,
                body_depth_coverage=coverage, stable=stable, reasons=reasons,
                recommended_action=action, pose_available=pose_available,
                pose_detected=bool(pose and pose.detected), landmarks_2d=landmarks,
            )
        except Exception as exc:
            logger.error(f"Failed to analyze capture quality: {exc}")
            return DistanceInfo(0, DistanceStatus.NO_DATA, 0, f"分析失败: {exc}", reasons=[str(exc)])

    def detect_human(self, depth_frame, depth_scale=1.0, color_frame=None):
        info = self.analyze_distance(depth_frame, color_frame, depth_scale)
        return info.status not in {DistanceStatus.NO_DATA, DistanceStatus.NO_HUMAN}, self._last_bbox

    def get_body_segmentation(self):
        """Return a defensive snapshot corresponding to the latest analysis."""
        mask = None if self._last_mask is None else self._last_mask.astype(np.uint8, copy=True)
        bbox = None if self._last_bbox is None else tuple(int(value) for value in self._last_bbox)
        return mask, bbox, self._last_mask_source

    def build_pose_metadata(self, info: DistanceInfo, depth_frame, depth_scale, intrinsics) -> dict:
        landmarks_3d = []
        h, w = depth_frame.shape
        for index, item in enumerate(info.landmarks_2d):
            u = int(np.clip(item["x"] * w, 0, w - 1))
            v = int(np.clip(item["y"] * h, 0, h - 1))
            patch = depth_frame[max(0, v - 2):min(h, v + 3), max(0, u - 2):min(w, u + 3)]
            valid = patch[patch > 0]
            z = float(np.median(valid) * depth_scale) if valid.size else 0.0
            x = (u - intrinsics.cx) * z / intrinsics.fx if z else 0.0
            y = (v - intrinsics.cy) * z / intrinsics.fy if z else 0.0
            landmarks_3d.append({"index": index, "x_mm": x, "y_mm": y, "z_mm": z})
        return {"landmarks_2d": info.landmarks_2d, "landmarks_3d": landmarks_3d}

    def get_distance_hint(self, info: DistanceInfo) -> str:
        return info.recommended_action or info.message

    def reset(self):
        self.history.clear()
        self.landmark_history.clear()
        self._last_mask = None
        self._last_bbox = None
        self._last_mask_source = None
        self.pose_analyzer.reset()

    def close(self):
        self.pose_analyzer.close()
