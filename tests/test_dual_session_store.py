import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

import numpy as np
import cv2

from backend.core.camera_adapters import CameraIntrinsicsData, FrameBundle
from backend.core.dual_session_store import DualSessionStore
from backend.protocol import measurement_definitions
from backend.storage.ply_writer import PLYWriter


def frame(index):
    color = np.zeros((4, 5, 3), dtype=np.uint8)
    color[..., 0] = 255  # RGB 红色，验证落盘时转换为 OpenCV BGR。
    return FrameBundle(
        color=color,
        depth_raw=np.full((4, 5), 1000 + index, dtype=np.uint16),
        depth_aligned=np.full((4, 5), 1000 + index, dtype=np.uint16),
        infrared={
            "left": np.full((4, 5), index, dtype=np.uint8),
            "right": np.full((4, 5), index + 1, dtype=np.uint8),
        },
        intrinsics={
            "depth_aligned": CameraIntrinsicsData(
                fx=5.0, fy=5.0, cx=2.0, cy=1.5, width=5, height=4,
            ),
        },
    )


class DualSessionStoreTests(unittest.TestCase):
    def test_creates_and_commits_an_eight_angle_group(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DualSessionStore(Path(directory))
            created = store.create_session("S0001", clothing_note="外套", target_distance_mm=2300)
            self.assertEqual(len(created["angles"]), 8)
            committed = store.commit_group(
                "S0001", 0, [frame(i) for i in range(5)], [frame(i) for i in range(5)],
                audit={"max_host_timestamp_skew_ms": 12.5}, metadata={"distance_mm": 2300},
            )
            self.assertEqual(committed["group_id"], "V000")
            state = store.get_session("S0001")
            self.assertEqual(state["angles"]["V000"]["status"], "CAPTURED")
            self.assertTrue((store.root / "README_数据格式说明.txt").exists())
            self.assertTrue((store.root / "subjects" / "S0001" / "session_manifest.json").exists())
            capture = store.root / "subjects" / "S0001" / "angles" / "angle_000_front" / committed["attempt_id"] / committed["capture"]["files"][0]["path"]
            self.assertTrue(capture.exists())
            self.assertIn("camera_gemini_336l/rgb_color/frame_01.png", committed["capture"]["files"][0]["path"])
            self.assertEqual(len(committed["capture"]["files"]), 60)  # 每相机 5 帧：RGB、两类深度、两类伪彩、PLY
            color = cv2.imdecode(np.frombuffer(capture.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
            self.assertTrue(np.array_equal(color[0, 0], np.array([0, 0, 255], dtype=np.uint8)))
            ply = capture.parent.parent / "pointcloud_color_xyz_mm" / "frame_01.ply"
            points, colors = PLYWriter.load(str(ply))
            self.assertGreater(len(points), 0)
            self.assertEqual(colors.shape[1], 3)
            self.assertTrue(np.array_equal(colors[0], np.array([255, 0, 0], dtype=np.uint8)))

    def test_eight_angles_measurements_and_completion_share_one_session(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DualSessionStore(Path(directory))
            store.create_session("S0002", clothing_note="长袖", target_distance_mm=2500)
            for yaw in range(0, 360, 45):
                store.commit_group(
                    "S0002", yaw,
                    [frame(i) for i in range(5)],
                    [frame(i) for i in range(5)],
                    audit={"max_host_timestamp_skew_ms": 10.0},
                    metadata={"distance_mm": 2500},
                )
            definitions = [asdict(item) for item in measurement_definitions()]
            records = []
            for definition in definitions:
                if not definition["required"]:
                    continue
                for field_name in definition["field_names"]:
                    records.append({
                        "measurement_id": definition["measurement_id"],
                        "field_name": field_name,
                        "m1": 100.0,
                        "m2": 100.0,
                    })
            measured = store.save_anthropometry("S0002", records, definitions)
            self.assertTrue(measured["anthropometry"]["complete"])
            self.assertTrue(measured["completion"]["can_complete"])
            completed = store.complete_session("S0002")
            self.assertEqual(completed["status"], "COMPLETE")
            self.assertTrue(completed["completion"]["completed"])


if __name__ == "__main__":
    unittest.main()
