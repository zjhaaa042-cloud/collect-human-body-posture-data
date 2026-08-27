import unittest

import numpy as np

from backend.core.camera_adapters import FrameBundle
from backend.core.dual_capture import DualCameraCaptureCoordinator, DualCameraCaptureError


class FakeDualCamera:
    def __init__(self, camera_code, timestamps):
        self.camera_code = camera_code
        self.timestamps = iter(timestamps)

    def get_status(self):
        return {"connected": True, "device": {"camera_code": self.camera_code}}

    def get_frames(self, timeout_ms):
        del timeout_ms
        return FrameBundle(
            color=np.zeros((2, 2, 3), dtype=np.uint8),
            host_timestamp_ns=next(self.timestamps),
        )


class DualCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_captures_pairs_and_reports_host_clock_audit(self):
        coordinator = DualCameraCaptureCoordinator(
            FakeDualCamera("C336L", [1_000_000_000, 2_000_000_000]),
            FakeDualCamera("CD435I", [1_015_000_000, 2_020_000_000]),
        )
        burst = await coordinator.capture_burst(
            frame_count=2, interval_ms=0, max_host_timestamp_skew_ms=30,
        )
        self.assertEqual(len(burst.pairs), 2)
        self.assertEqual(burst.audit_payload()["synchronization_kind"], "host_clock_near_sync")
        self.assertEqual(burst.max_host_timestamp_skew_ms, 20.0)

    async def test_rejects_pairs_outside_configured_skew(self):
        coordinator = DualCameraCaptureCoordinator(
            FakeDualCamera("C336L", [1_000_000_000]),
            FakeDualCamera("CD435I", [1_200_000_000]),
        )
        with self.assertRaisesRegex(DualCameraCaptureError, "超过"):
            await coordinator.capture_burst(frame_count=1, interval_ms=0, max_host_timestamp_skew_ms=50)


if __name__ == "__main__":
    unittest.main()
