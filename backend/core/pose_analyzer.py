import threading
import time
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import numpy as np
from loguru import logger

try:
    _MPL_CACHE = Path(tempfile.gettempdir()) / "body_posture_collector" / "matplotlib"
    _MPL_CACHE.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(_MPL_CACHE))
except OSError:
    # Pose is optional; a read-only or restricted temporary directory must not
    # prevent the backend from importing.
    pass
os.environ.setdefault("GLOG_minloglevel", "3")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

try:
    import mediapipe as mp
    HAS_MEDIAPIPE = True
except ImportError:
    mp = None
    HAS_MEDIAPIPE = False

@dataclass
class PoseFrameResult:
    timestamp_ms: int
    landmarks: List[dict] = field(default_factory=list)
    segmentation_mask: Optional[np.ndarray] = None
    detected: bool = False


class PoseAnalyzer:
    """MediaPipe live-frame worker with a one-element latest-frame queue."""

    def __init__(self, model_path: str, inference_interval_ms: int = 100):
        self.model_path = Path(model_path)
        if not self.model_path.is_absolute():
            self.model_path = Path(__file__).resolve().parents[2] / self.model_path
        self.inference_interval_ms = max(50, int(inference_interval_ms))
        self.available = False
        self.error = ""
        self._landmarker = None
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._stop_event = threading.Event()
        self._pending = None
        self._latest = None
        self._generation = 0
        self._last_submit_ms = 0
        self._thread = None
        self._initialize()

    def _initialize(self):
        if not HAS_MEDIAPIPE:
            self.error = "MediaPipe 未安装"
            logger.warning("Pose analysis unavailable: MediaPipe is not installed")
            return
        if not self.model_path.is_file():
            self.error = f"姿态模型不存在: {self.model_path}"
            logger.warning(f"Pose analysis unavailable: model not found at {self.model_path}")
            return
        try:
            options = mp.tasks.vision.PoseLandmarkerOptions(
                base_options=mp.tasks.BaseOptions(model_asset_path=str(self.model_path)),
                running_mode=mp.tasks.vision.RunningMode.VIDEO,
                num_poses=1,
                min_pose_detection_confidence=0.55,
                min_pose_presence_confidence=0.55,
                min_tracking_confidence=0.55,
                output_segmentation_masks=True,
            )
            self._landmarker = mp.tasks.vision.PoseLandmarker.create_from_options(options)
            self.available = True
            self._thread = threading.Thread(target=self._worker, name="pose-analyzer", daemon=True)
            self._thread.start()
            logger.info(f"MediaPipe pose analysis initialized: {self.model_path}")
        except Exception as exc:
            self.error = str(exc)
            logger.warning(f"Pose analysis unavailable: {exc}")

    def submit(self, rgb_frame: np.ndarray, timestamp_ms: Optional[int] = None, force: bool = False):
        if not self.available or rgb_frame is None or rgb_frame.size == 0:
            return None
        timestamp_ms = int(timestamp_ms or time.monotonic() * 1000)
        if force and timestamp_ms <= self._last_submit_ms:
            timestamp_ms = self._last_submit_ms + 1
        if not force and timestamp_ms - self._last_submit_ms < self.inference_interval_ms:
            return None
        self._last_submit_ms = timestamp_ms
        with self._lock:
            generation = self._generation
            self._pending = (
                np.ascontiguousarray(rgb_frame),
                timestamp_ms,
                generation,
            )
        self._event.set()
        return timestamp_ms

    def get_latest(self, max_age_ms: int = 750) -> Optional[PoseFrameResult]:
        with self._lock:
            result = self._latest
        if result is None:
            return None
        if int(time.monotonic() * 1000) - result.timestamp_ms > max_age_ms:
            return None
        return result

    def wait_for_result(self, minimum_timestamp_ms: int, timeout: float = 0.8) -> Optional[PoseFrameResult]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                result = self._latest
            if result and result.timestamp_ms >= minimum_timestamp_ms:
                return result
            time.sleep(0.01)
        return None

    @staticmethod
    def _serialize_landmarks(items) -> List[dict]:
        return [
            {
                "x": float(item.x or 0.0),
                "y": float(item.y or 0.0),
                "z": float(item.z or 0.0),
                "visibility": float(item.visibility or 0.0),
                "presence": float(item.presence or 0.0),
            }
            for item in items
        ]

    def _worker(self):
        while not self._stop_event.is_set():
            self._event.wait(0.2)
            self._event.clear()
            if self._stop_event.is_set():
                break
            with self._lock:
                pending = self._pending
                self._pending = None
            if pending is None:
                continue
            frame, timestamp_ms, generation = pending
            try:
                image = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame)
                result = self._landmarker.detect_for_video(image, timestamp_ms)
                landmarks = self._serialize_landmarks(result.pose_landmarks[0]) if result.pose_landmarks else []
                mask = None
                if result.segmentation_masks:
                    mask = np.array(result.segmentation_masks[0].numpy_view(), dtype=np.float32, copy=True)
                    if mask.ndim == 3:
                        mask = mask[:, :, 0]
                pose_result = PoseFrameResult(
                    timestamp_ms=timestamp_ms,
                    landmarks=landmarks,
                    segmentation_mask=mask,
                    detected=bool(landmarks),
                )
                with self._lock:
                    if generation == self._generation:
                        self._latest = pose_result
            except Exception as exc:
                logger.debug(f"Pose inference failed: {exc}")

    def close(self):
        with self._lock:
            self._generation += 1
            self._pending = None
            self._latest = None
        self._stop_event.set()
        self._event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        worker_stopped = not self._thread or not self._thread.is_alive()
        if self._landmarker and worker_stopped:
            try:
                self._landmarker.close()
            except Exception:
                pass
        elif self._landmarker:
            logger.warning("Pose worker did not stop in time; landmarker close deferred")
        self.available = False

    def reset(self):
        with self._lock:
            self._generation += 1
            self._pending = None
            self._latest = None
        self._last_submit_ms = 0
