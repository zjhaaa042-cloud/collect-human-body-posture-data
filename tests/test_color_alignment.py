import base64
import unittest

import cv2
import numpy as np

from backend.core.camera_adapters import (
    CameraExtrinsicsData,
    CameraIntrinsicsData,
    FrameBundle,
)
from backend.core.frame_contract import FrameContractError, validate_frame_contract
from backend.utils.frame_processor import FrameProcessor


def valid_frame() -> FrameBundle:
    color = np.zeros((60, 90, 3), dtype=np.uint8)
    color[:, :30] = (255, 0, 0)
    color[:, 30:60] = (0, 255, 0)
    color[:, 60:] = (0, 0, 255)
    color_intrinsic = CameraIntrinsicsData(
        fx=80.0, fy=80.0, cx=44.5, cy=29.5, width=90, height=60,
    )
    depth_intrinsic = CameraIntrinsicsData(
        fx=79.0, fy=79.0, cx=44.0, cy=29.0, width=90, height=60,
    )
    return FrameBundle(
        color=color,
        depth_raw=np.full((60, 90), 1000, dtype=np.uint16),
        depth_aligned=np.full((60, 90), 1000, dtype=np.uint16),
        depth_scale=1.0,
        camera_metadata={"rgb_color_order": "RGB", "rgb_transfer": "sRGB"},
        intrinsics={
            "color": color_intrinsic,
            "depth_raw": depth_intrinsic,
            "depth_aligned": color_intrinsic,
        },
        extrinsics={
            "depth_raw_to_color": CameraExtrinsicsData(
                source="depth_raw",
                target="color",
                rotation=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
                translation=(0.01, 0.0, 0.0),
            )
        },
        stream_timestamps={
            "color": 1000.0,
            "depth_raw": 1000.2,
            "depth_aligned": 1000.2,
        },
        stream_frame_numbers={"color": 8, "depth_raw": 7, "depth_aligned": 7},
    )


class ColorAndAlignmentTests(unittest.TestCase):
    def test_preview_preserves_rgb_channel_semantics(self):
        frame = valid_frame()
        encoded = FrameProcessor(preview_size=(90, 60), jpeg_quality=100).encode_preview(
            frame.color, is_rgb=True
        )
        decoded_bgr = cv2.imdecode(
            np.frombuffer(base64.b64decode(encoded), dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        decoded_rgb = cv2.cvtColor(decoded_bgr, cv2.COLOR_BGR2RGB)
        samples = (decoded_rgb[30, 15], decoded_rgb[30, 45], decoded_rgb[30, 75])
        expected = (
            np.array([255, 0, 0]),
            np.array([0, 255, 0]),
            np.array([0, 0, 255]),
        )
        for actual, target in zip(samples, expected):
            self.assertLessEqual(int(np.abs(actual.astype(int) - target).max()), 3)

    def test_valid_contract_proves_spatial_and_temporal_alignment(self):
        result = validate_frame_contract(valid_frame(), "TEST")
        self.assertTrue(
            result["spatial_alignment"]["depth_aligned_matches_rgb_pixels"]
        )
        self.assertEqual(
            result["spatial_alignment"]["depth_raw_coordinate_system"],
            "native_depth_camera",
        )
        self.assertAlmostEqual(
            result["temporal_alignment"]["stream_timestamp_skew_ms"], 0.2
        )

    def test_contract_rejects_wrong_color_order(self):
        frame = valid_frame()
        frame.camera_metadata["rgb_color_order"] = "BGR"
        with self.assertRaisesRegex(FrameContractError, "色序"):
            validate_frame_contract(frame, "TEST")

    def test_contract_rejects_false_aligned_intrinsics(self):
        frame = valid_frame()
        frame.intrinsics["depth_aligned"] = frame.intrinsics["depth_raw"]
        with self.assertRaisesRegex(FrameContractError, "aligned depth"):
            validate_frame_contract(frame, "TEST")

    def test_contract_rejects_unpaired_raw_and_aligned_depth(self):
        frame = valid_frame()
        frame.stream_frame_numbers["depth_aligned"] = 99
        with self.assertRaisesRegex(FrameContractError, "同一深度帧"):
            validate_frame_contract(frame, "TEST")


if __name__ == "__main__":
    unittest.main()
