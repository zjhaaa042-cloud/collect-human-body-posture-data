import tempfile
import unittest
from pathlib import Path

import numpy as np

from backend.core.camera_manager import CameraIntrinsics, CameraManager, FrameData
from backend.core.data_collector import CaptureConfig, DataCollector
from backend.core.depth_analyzer import DepthAnalyzer, DistanceStatus
from backend.core.pose_analyzer import PoseFrameResult


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
    def test_v2_metadata_and_pose_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            collector = DataCollector(temp_dir)
            collector.create_session("test")
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
                quality_snapshot={"score": 90, "distance_mm": 1500},
                pose_metadata={"landmarks_2d": [], "landmarks_3d": []},
                orientation_metadata={"orientation": "portrait_cw"},
            )
            self.assertTrue(result.success)
            self.assertTrue((Path(temp_dir) / "sessions" / "test" / result.pose_path).is_file())
            self.assertEqual(collector.session_metadata["format_version"], 2)
            self.assertEqual(collector.session_metadata["processing"]["orientation"], "portrait_cw")


if __name__ == "__main__":
    unittest.main()
