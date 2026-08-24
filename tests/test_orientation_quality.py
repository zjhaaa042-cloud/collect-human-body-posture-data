import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np

from backend.core.camera_manager import CameraIntrinsics, CameraManager, FrameData
from backend.core.data_collector import CaptureConfig, DataCollector
from backend.core.depth_analyzer import DepthAnalyzer, DistanceStatus
from backend.core.pose_analyzer import PoseAnalyzer, PoseFrameResult
from backend.storage.ply_writer import PLYWriter


class FakePoseAnalyzer:
    available = True

    def __init__(self, result):
        self.result = result

    def submit(self, *_args, **_kwargs):
        return None

    def get_latest(self, *_args, **_kwargs):
        return self.result

    def close(self):
        return None


class OrientationTests(unittest.TestCase):
    def setUp(self):
        self.raw = CameraIntrinsics(600.0, 620.0, 639.5, 399.5, 1280, 800)

    def test_clockwise_intrinsics_preserve_projection(self):
        transformed = CameraManager.transform_intrinsics(self.raw, "portrait_cw")
        u, v, z = 720.0, 300.0, 1500.0
        x = (u - self.raw.cx) * z / self.raw.fx
        y = (v - self.raw.cy) * z / self.raw.fy
        rotated_u, rotated_v = self.raw.height - 1 - v, u
        rotated_x = (rotated_u - transformed.cx) * z / transformed.fx
        rotated_y = (rotated_v - transformed.cy) * z / transformed.fy
        self.assertAlmostEqual(rotated_x, -y)
        self.assertAlmostEqual(rotated_y, x)
        self.assertEqual((transformed.width, transformed.height), (800, 1280))

    def test_counterclockwise_intrinsics_preserve_projection(self):
        transformed = CameraManager.transform_intrinsics(self.raw, "portrait_ccw")
        u, v, z = 720.0, 300.0, 1500.0
        x = (u - self.raw.cx) * z / self.raw.fx
        y = (v - self.raw.cy) * z / self.raw.fy
        rotated_u, rotated_v = v, self.raw.width - 1 - u
        rotated_x = (rotated_u - transformed.cx) * z / transformed.fx
        rotated_y = (rotated_v - transformed.cy) * z / transformed.fy
        self.assertAlmostEqual(rotated_x, y)
        self.assertAlmostEqual(rotated_y, -x)

    def test_rgb_and_depth_rotate_together(self):
        camera = CameraManager("portrait_cw")
        depth = np.arange(12, dtype=np.uint16).reshape(3, 4)
        color = np.repeat(depth[:, :, None].astype(np.uint8), 3, axis=2)
        rotated_depth = camera._rotate_array(depth)
        rotated_color = camera._rotate_array(color)
        self.assertTrue(np.array_equal(rotated_depth, rotated_color[:, :, 0]))
        self.assertEqual(rotated_depth.shape, (4, 3))


class PoseFreshnessTests(unittest.TestCase):
    def test_wait_for_result_never_falls_back_to_an_older_frame(self):
        analyzer = PoseAnalyzer.__new__(PoseAnalyzer)
        analyzer._lock = threading.Lock()
        now_ms = int(time.monotonic() * 1000)
        analyzer._latest = PoseFrameResult(now_ms, detected=True)

        result = analyzer.wait_for_result(now_ms + 1, timeout=0.0)

        self.assertIsNone(result)

    def test_reset_invalidates_inflight_generation(self):
        analyzer = PoseAnalyzer.__new__(PoseAnalyzer)
        analyzer._lock = threading.Lock()
        analyzer._generation = 7
        analyzer._pending = (object(), 1, 7)
        analyzer._latest = PoseFrameResult(1, detected=True)
        analyzer._last_submit_ms = 1

        analyzer.reset()

        self.assertEqual(analyzer._generation, 8)
        self.assertIsNone(analyzer._pending)
        self.assertIsNone(analyzer._latest)
        self.assertEqual(analyzer._last_submit_ms, 0)


class QualityTests(unittest.TestCase):
    @staticmethod
    def _pose_result():
        landmarks = []
        for index in range(33):
            landmarks.append({
                "x": 0.35 + (index % 2) * 0.3,
                "y": 0.10 + (index / 32) * 0.80,
                "z": 0.0,
                "visibility": 0.95,
                "presence": 0.95,
            })
        mask = np.zeros((1280, 800), dtype=np.float32)
        mask[100:1180, 80:720] = 1.0
        return PoseFrameResult(1, landmarks, mask, True)

    def test_ready_after_stable_pose(self):
        analyzer = DepthAnalyzer(pose_model_path="missing.task")
        analyzer.pose_analyzer.close()
        analyzer.pose_analyzer = FakePoseAnalyzer(self._pose_result())
        depth = np.full((1280, 800), 2500, dtype=np.uint16)
        depth[100:1180, 80:720] = 1500
        yy, xx = np.indices((1280, 800))
        checker = (((xx // 8 + yy // 8) % 2) * 180 + 30).astype(np.uint8)
        color = np.repeat(checker[:, :, None], 3, axis=2)
        info = None
        for _ in range(5):
            info = analyzer.analyze_distance(depth, color)
        self.assertTrue(info.ready, info.to_capture_quality())
        self.assertEqual(info.status, DistanceStatus.OPTIMAL)
        self.assertGreaterEqual(info.score, 80)
        self.assertGreaterEqual(info.body_depth_coverage, 0.75)

    def test_edge_clipping_blocks_capture(self):
        result = self._pose_result()
        result.segmentation_mask[:, :100] = 1.0
        analyzer = DepthAnalyzer(pose_model_path="missing.task")
        analyzer.pose_analyzer.close()
        analyzer.pose_analyzer = FakePoseAnalyzer(result)
        depth = np.full((1280, 800), 1500, dtype=np.uint16)
        color = np.zeros((1280, 800, 3), dtype=np.uint8)
        info = analyzer.analyze_distance(depth, color)
        self.assertFalse(info.ready)
        self.assertEqual(info.status, DistanceStatus.BODY_INCOMPLETE)


class StorageTests(unittest.TestCase):
    @staticmethod
    def _calibration(orientation="portrait_cw"):
        return {
            "calibration_version": "test_v1",
            "camera": {"serial_number": "TEST123"},
            "orientation": orientation,
            "raw_resolution": [3, 4],
            "output_resolution": [3, 4],
            "output_intrinsics": {"fx": 3, "fy": 3, "cx": 1, "cy": 2, "width": 3, "height": 4},
            "depth_unit_mm": 1.0,
            "alignment": "depth_to_color",
        }

    @staticmethod
    def _context():
        return {
            "capture_group_id": "group_001",
            "view_yaw_deg": 0,
            "pose_type": "standing_relaxed",
            "clothing_type": "fitted",
            "camera_height_mm": 900,
            "quality_ready": True,
        }

    def test_v3_metadata_mask_calibration_and_truth(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            collector = DataCollector(temp_dir)
            collector.create_session("test", {
                "subject_id": "subject_001",
                "visit_id": "visit_001",
                "measurement": {"raw_readings_cm": [72.1, 72.3, 72.2], "measurer_id": "staff_01"},
            })
            frame = FrameData(
                color=np.full((4, 3, 3), 120, dtype=np.uint8),
                depth=np.full((4, 3), 1500, dtype=np.uint16),
                depth_scale=1.0,
                timestamp=1,
                frame_number=1,
            )
            intrinsics = CameraIntrinsics(3, 3, 1, 2, 3, 4, "portrait_cw")
            result = collector.capture(
                frame,
                config=CaptureConfig(save_pointcloud=False, quality_check=False),
                camera_intrinsics=intrinsics,
                quality_snapshot={"score": 90, "distance_mm": 1500, "ready": True},
                pose_metadata={"landmarks_2d": [], "landmarks_3d": []},
                calibration_snapshot=self._calibration(),
                body_mask=np.ones((4, 3), dtype=np.uint8),
                body_bbox=(0, 0, 3, 4),
                mask_source="mediapipe",
                capture_context=self._context(),
            )
            self.assertTrue(result.success)
            self.assertTrue((Path(temp_dir) / "sessions" / "test" / result.pose_path).is_file())
            self.assertTrue((Path(temp_dir) / "sessions" / "test" / result.mask_path).is_file())
            self.assertTrue((Path(temp_dir) / "sessions" / "test" / result.calibration_path).is_file())
            self.assertEqual(collector.session_metadata["format_version"], 3)
            self.assertEqual(collector.session_metadata["measurement"]["mean_cm"], 72.2)
            self.assertEqual(collector.session_metadata["captures"][0]["qc_status"], "accepted")
            collector.update_capture_review("cap_001", "isolated", "staff_01", "衣物遮挡")
            self.assertEqual(collector.session_metadata["captures"][0]["qc_status"], "isolated")
            self.assertEqual(collector.session_metadata["captures"][0]["manual_review"]["decision"], "isolated")

    def test_configuration_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            collector = DataCollector(temp_dir)
            collector.create_session("test", {"subject_id": "subject_001", "visit_id": "visit_001"})
            frame = FrameData(None, np.full((4, 3), 1500, dtype=np.uint16), 1.0, 1, 1)
            config = CaptureConfig(save_rgb=False, save_pointcloud=False, quality_check=False)
            first = collector.capture(
                frame, config=config, calibration_snapshot=self._calibration(), body_mask=np.ones((4, 3)), capture_context=self._context()
            )
            second = collector.capture(
                frame, config=config, calibration_snapshot=self._calibration("landscape"), body_mask=np.ones((4, 3)), capture_context=self._context()
            )
            self.assertTrue(first.success)
            self.assertFalse(second.success)
            self.assertIn("请新建会话", second.error)

    def test_binary_and_ascii_ply_round_trip(self):
        points = np.array([[1.5, 2.5, 3.5], [-1.0, 0.0, 4.0]], dtype=np.float32)
        colors = np.array([[10, 20, 30], [200, 210, 220]], dtype=np.uint8)
        with tempfile.TemporaryDirectory() as temp_dir:
            for binary in (False, True):
                path = Path(temp_dir) / f"roundtrip_{binary}.ply"
                PLYWriter.save(str(path), points, colors, binary=binary)
                loaded_points, loaded_colors = PLYWriter.load(str(path))
                np.testing.assert_allclose(loaded_points, points, atol=1e-3)
                np.testing.assert_array_equal(loaded_colors, colors)


if __name__ == "__main__":
    unittest.main()
