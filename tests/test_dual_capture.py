import unittest

import numpy as np

from backend.core.camera_adapters import (
    CameraExtrinsicsData,
    CameraIntrinsicsData,
    FrameBundle,
)
from backend.core.dual_capture import DualCameraCaptureCoordinator, DualCameraCaptureError


class FakeDualCamera:
    def __init__(self, camera_code, timestamps):
        self.camera_code = camera_code
        self.timestamps = iter(timestamps)

    def get_status(self):
        return {"connected": True, "device": {"camera_code": self.camera_code}}

    def get_frames(self, timeout_ms):
        del timeout_ms
        intrinsic = CameraIntrinsicsData(
            fx=2.0, fy=2.0, cx=0.5, cy=0.5, width=2, height=2,
        )
        frame_number = int(next(self.timestamps))
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
                "color": float(frame_number),
                "depth_raw": float(frame_number),
                "depth_aligned": float(frame_number),
            },
            stream_frame_numbers={
                "color": frame_number,
                "depth_raw": frame_number,
                "depth_aligned": frame_number,
            },
            host_timestamp_ns=frame_number,
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
