import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

import numpy as np
import cv2

from backend.core.camera_adapters import CameraIntrinsicsData, FrameBundle
from backend.core.dual_session_store import DualSessionStore, DualSessionStoreError
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
            self.assertEqual(len(committed["capture"]["files"]), 80)  # 每相机 5 帧：原有 6 文件 + 两类 NPY
            color = cv2.imdecode(np.frombuffer(capture.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
            self.assertTrue(np.array_equal(color[0, 0], np.array([0, 0, 255], dtype=np.uint8)))
            ply = capture.parent.parent / "pointcloud_color_xyz_mm" / "frame_01.ply"
            points, colors = PLYWriter.load(str(ply))
            self.assertGreater(len(points), 0)
            self.assertEqual(colors.shape[1], 3)
            self.assertTrue(np.array_equal(colors[0], np.array([255, 0, 0], dtype=np.uint8)))
            npy_record = next(
                item for item in committed["capture"]["files"]
                if item["camera_code"] == "C336L"
                and item["frame"] == "frame_01"
                and item["modality"] == "depth_raw_npy"
            )
            npy_path = capture.parents[2] / npy_record["path"]
            depth_npy = np.load(npy_path, allow_pickle=False)
            self.assertEqual(depth_npy.dtype, np.uint16)
            self.assertEqual(npy_record["depth_scale_mm_per_unit"], 1.0)
            depth_png = cv2.imdecode(
                np.frombuffer(
                    (capture.parents[2] / "camera_gemini_336l/depth_raw_uint16/frame_01.png").read_bytes(),
                    dtype=np.uint8,
                ),
                cv2.IMREAD_UNCHANGED,
            )
            np.testing.assert_array_equal(depth_npy, depth_png)
            attempt_dir = capture.parents[2]
            self.assertTrue((attempt_dir / "commit.json").is_file())
            self.assertEqual(len(committed["capture"]["frames"]["C336L"]), 5)
            self.assertIn("intrinsics", committed["capture"]["frames"]["C336L"][0])

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
            with self.assertRaisesRegex(DualSessionStoreError, "完成"):
                store.commit_group(
                    "S0002", 0,
                    [frame(i) for i in range(5)],
                    [frame(i) for i in range(5)],
                    audit={"max_host_timestamp_skew_ms": 10.0},
                    metadata={"distance_mm": 2500},
                )

    def test_recovers_a_verified_final_attempt_missing_from_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DualSessionStore(Path(directory))
            store.create_session("S0003")
            committed = store.commit_group(
                "S0003", 0,
                [frame(i) for i in range(5)],
                [frame(i) for i in range(5)],
                audit={"max_host_timestamp_skew_ms": 9.0}, metadata={},
            )
            state_path = store.root / "subjects" / "S0003" / "session_manifest.json"
            state = store._read_json(state_path)
            state["angles"]["V000"] = {"yaw_deg": 0, "status": "PENDING", "attempts": []}
            store._atomic_json(state_path, state)

            recovered = store.get_session("S0003")
            self.assertEqual(recovered["angles"]["V000"]["status"], "CAPTURED")
            self.assertEqual(
                recovered["angles"]["V000"]["attempts"][0]["attempt_id"],
                committed["attempt_id"],
            )
            self.assertEqual(recovered["recovery_report"]["recovered_attempts"], 1)
            second = store.get_session("S0003")
            self.assertEqual(len(second["angles"]["V000"]["attempts"]), 1)

    def test_recovers_pre_upgrade_complete_staging_without_commit_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DualSessionStore(Path(directory))
            store.create_session("S0006")
            committed = store.commit_group(
                "S0006", 0,
                [frame(i) for i in range(5)], [frame(i) for i in range(5)],
                audit={"max_host_timestamp_skew_ms": 7.0}, metadata={},
            )
            subject_dir = store.root / "subjects" / "S0006"
            final_dir = subject_dir / "angles" / "angle_000_front" / committed["attempt_id"]
            manifest_path = final_dir / "capture_manifest.json"
            manifest = store._read_json(manifest_path)
            manifest.pop("storage_features", None)
            store._atomic_json(manifest_path, manifest)
            (final_dir / "commit.json").unlink()
            staging = subject_dir / ".staging" / committed["attempt_id"]
            final_dir.replace(staging)
            state_path = subject_dir / "session_manifest.json"
            state = store._read_json(state_path)
            state["angles"]["V000"] = {"yaw_deg": 0, "status": "PENDING", "attempts": []}
            store._atomic_json(state_path, state)

            recovered = store.get_session("S0006")
            self.assertEqual(recovered["angles"]["V000"]["status"], "CAPTURED")
            self.assertFalse(staging.exists())
            self.assertTrue(final_dir.exists())

    def test_partial_staging_is_preserved_and_blocks_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DualSessionStore(Path(directory))
            store.create_session("S0004")
            partial = store.root / "subjects" / "S0004" / ".staging" / "capture_partial"
            partial.mkdir()
            state = store.get_session("S0004")
            self.assertTrue(partial.exists())
            self.assertTrue(state["reconciliation_required"])
            with self.assertRaisesRegex(DualSessionStoreError, "待恢复|完整性"):
                store.commit_group(
                    "S0004", 0,
                    [frame(i) for i in range(5)],
                    [frame(i) for i in range(5)],
                    audit={"max_host_timestamp_skew_ms": 10.0}, metadata={},
                )

    def test_manifest_path_cannot_escape_attempt_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DualSessionStore(Path(directory))
            store.create_session("S0005")
            committed = store.commit_group(
                "S0005", 0,
                [frame(i) for i in range(5)],
                [frame(i) for i in range(5)],
                audit={"max_host_timestamp_skew_ms": 8.0}, metadata={},
            )
            attempt_dir = (
                store.root / "subjects" / "S0005" / "angles" / "angle_000_front"
                / committed["attempt_id"]
            )
            manifest_path = attempt_dir / "capture_manifest.json"
            commit_path = attempt_dir / "commit.json"
            manifest = store._read_json(manifest_path)
            manifest["files"][0]["path"] = "../../escape.png"
            store._atomic_json(manifest_path, manifest)
            commit = store._read_json(commit_path)
            commit["files"] = manifest["files"]
            commit["capture_manifest"]["sha256"] = store._sha256(manifest_path)
            store._atomic_json(commit_path, commit)
            with self.assertRaisesRegex(DualSessionStoreError, "越出"):
                store._verify_attempt_directory(attempt_dir, require_commit=True)


if __name__ == "__main__":
    unittest.main()
