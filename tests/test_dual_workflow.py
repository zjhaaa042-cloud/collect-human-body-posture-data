import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from backend.application.dual_workflow import DualWorkflowService
from backend.core.camera_adapters import (
    CameraExtrinsicsData,
    CameraIntrinsicsData,
    FrameBundle,
)


class FakeCamera:
    def __init__(self, camera_code, lock):
        self.camera_code = camera_code
        self.lock = lock
        self.calls = 0
        self.observed_locked = []

    def get_status(self):
        return {"connected": True, "device": {"camera_code": self.camera_code}}

    def get_frames(self, timeout_ms):
        del timeout_ms
        self.observed_locked.append(self.lock.locked())
        self.calls += 1
        intrinsic = CameraIntrinsicsData(
            fx=2.0, fy=2.0, cx=0.5, cy=0.5, width=2, height=2,
        )
        return FrameBundle(
            color=np.zeros((2, 2, 3), dtype=np.uint8),
            depth_raw=np.full((2, 2), 1000, dtype=np.uint16),
            depth_aligned=np.full((2, 2), 1000, dtype=np.uint16),
            camera_metadata={"rgb_color_order": "RGB"},
            intrinsics={
                "color": intrinsic,
                "depth_raw": intrinsic,
                "depth_aligned": intrinsic,
            },
            extrinsics={
                "depth_raw_to_color": CameraExtrinsicsData(
                    source="depth_raw",
                    target="color",
                    rotation=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
                    translation=(0.0, 0.0, 0.0),
                )
            },
            stream_timestamps={
                "color": float(self.calls),
                "depth_raw": float(self.calls),
                "depth_aligned": float(self.calls),
            },
            stream_frame_numbers={
                "color": self.calls,
                "depth_raw": self.calls,
                "depth_aligned": self.calls,
            },
            host_timestamp_ns=1_000_000_000 + self.calls * 1_000_000,
        )


class DualWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_subject_must_match_active_session(self):
        with tempfile.TemporaryDirectory() as directory:
            service = DualWorkflowService(lambda: (None, None))
            service.create_session(
                subject_id="S0001", output_path=directory,
            )
            with self.assertRaisesRegex(ValueError, "当前活动任务不一致"):
                service.complete_session(subject_id="S0002")
            service.close()

    async def test_formal_burst_holds_camera_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            camera_lock = asyncio.Lock()
            gemini = FakeCamera("C336L", camera_lock)
            d435i = FakeCamera("CD435I", camera_lock)
            service = DualWorkflowService(lambda: (gemini, d435i))
            service.create_session(subject_id="S0001", output_path=directory)
            committed = {
                "attempt_id": "capture_test",
                "state": {},
                "capture": {},
            }
            with mock.patch.object(service.store, "commit_group", return_value=committed):
                result = await service.capture_group(
                    subject_id="S0001",
                    yaw_deg=0,
                    distance_mm=2500,
                    ready=True,
                    capture_lock=asyncio.Lock(),
                    camera_lock=camera_lock,
                    set_capturing=lambda value: None,
                    settle_seconds=0,
                    interval_ms=0,
                )
            self.assertEqual(result["attempt_id"], "capture_test")
            self.assertEqual(gemini.calls, 5)
            self.assertEqual(d435i.calls, 5)
            self.assertTrue(all(gemini.observed_locked + d435i.observed_locked))
            service.close()


if __name__ == "__main__":
    unittest.main()
