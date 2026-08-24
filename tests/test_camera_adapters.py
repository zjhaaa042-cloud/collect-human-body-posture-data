import unittest
from types import SimpleNamespace

import numpy as np

from backend.core.camera_adapters import (
    CameraAdapterRegistry,
    OrbbecCameraAdapter,
    RealSenseCameraAdapter,
)


class FakeOrbbecManager:
    def __init__(self):
        self.last_error = ""
        self.pipeline = None
        self.connected = False
        self.connect_arguments = {}
        self.released = False
        self.color = np.full((2, 3, 3), 17, dtype=np.uint8)
        self.depth = np.full((2, 3), 2500, dtype=np.uint16)

    def list_devices(self):
        return [
            {
                "id": "ORB123",
                "index": 0,
                "name": "Gemini 336L",
                "serial_number": "ORB123",
            }
        ]

    def get_status(self):
        return {
            "sdk_available": True,
            "device_present": True,
            "connected": self.connected,
            "device": self.get_device_info(),
            "devices": self.list_devices(),
            "message": "ok",
        }

    def connect(self, **kwargs):
        self.connect_arguments = kwargs
        self.connected = True
        return True

    def release(self):
        self.released = True
        self.connected = False

    def get_device_info(self):
        return self.list_devices()[0]

    def get_frames(self):
        return SimpleNamespace(
            color=self.color,
            depth=self.depth,
            depth_scale=1.0,
            timestamp=99,
            frame_number=3,
        )

    def get_camera_intrinsics(self):
        return SimpleNamespace(fx=600, fy=601, cx=320, cy=240, width=640, height=480)


class FakeIntrinsics:
    fx = 600.0
    fy = 601.0
    ppx = 320.0
    ppy = 240.0
    width = 640
    height = 480
    model = "brown_conrady"
    coeffs = [0.1, 0.01, 0.0, 0.0, 0.001]


class FakeExtrinsics:
    rotation = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    translation = [0.01, 0.0, 0.0]


class FakeVideoProfile:
    def as_video_stream_profile(self):
        return self

    def get_intrinsics(self):
        return FakeIntrinsics()

    def get_extrinsics_to(self, target):
        del target
        return FakeExtrinsics()

    def width(self):
        return 640

    def height(self):
        return 480

    def fps(self):
        return 30

    def format(self):
        return "rgb8"


class FakeFrame:
    def __init__(self, data, timestamp=1234.5, frame_number=42):
        self._data = data
        self._timestamp = timestamp
        self._frame_number = frame_number
        self.profile = FakeVideoProfile()

    def get_data(self):
        return self._data

    def get_timestamp(self):
        return self._timestamp

    def get_frame_number(self):
        return self._frame_number

    def supports_frame_metadata(self, key):
        del key
        return False


class FakeFrameSet:
    def __init__(
        self,
        color,
        depth,
        left=None,
        right=None,
        timestamp=9999.0,
        frame_number=0,
    ):
        self.color = color
        self.depth = depth
        self.left = left
        self.right = right
        self.timestamp = timestamp
        self.frame_number = frame_number

    def get_color_frame(self):
        return self.color

    def get_depth_frame(self):
        return self.depth

    def get_infrared_frame(self, index):
        return self.left if index == 1 else self.right

    def get_timestamp(self):
        return self.timestamp

    def get_frame_number(self):
        return self.frame_number


class FakeDevice:
    INFO = {
        "serial_number": "RS123",
        "name": "Intel RealSense D435I",
        "firmware_version": "5.16.0",
        "product_line": "D400",
        "product_id": "0B3A",
        "usb_type_descriptor": "3.2",
    }

    def supports(self, key):
        return key in self.INFO

    def get_info(self, key):
        return self.INFO[key]

    def first_depth_sensor(self):
        return SimpleNamespace(get_depth_scale=lambda: 0.001)


class FakeContext:
    def __init__(self, device):
        self.device = device

    def query_devices(self):
        return [self.device]


class FakeConfig:
    def __init__(self):
        self.device_serial = ""
        self.streams = []

    def enable_device(self, serial):
        self.device_serial = serial

    def enable_stream(self, *args):
        self.streams.append(args)


class FakePipeline:
    def __init__(self, device, frames, wait_error=None):
        self.device = device
        self.frames = frames
        self.wait_error = wait_error
        self.started = False
        self.stopped = False

    def start(self, config):
        del config
        self.started = True
        return SimpleNamespace(get_device=lambda: self.device)

    def stop(self):
        self.stopped = True

    def wait_for_frames(self, timeout_ms):
        if timeout_ms <= 0:
            raise RuntimeError("invalid timeout")
        if self.wait_error is not None:
            raise self.wait_error
        return self.frames


class FakeAlign:
    def __init__(self, frames):
        self.frames = frames

    def process(self, raw_frames):
        del raw_frames
        return self.frames


class FakeRealSenseModule:
    __version__ = "2.55.1"
    camera_info = SimpleNamespace(
        serial_number="serial_number",
        name="name",
        firmware_version="firmware_version",
        product_line="product_line",
        product_id="product_id",
        usb_type_descriptor="usb_type_descriptor",
    )
    stream = SimpleNamespace(depth="depth", color="color", infrared="infrared")
    format = SimpleNamespace(z16="z16", rgb8="rgb8", y8="y8")

    def __init__(self, wait_error=None):
        self.device = FakeDevice()
        color = FakeFrame(np.full((2, 3, 3), 10, dtype=np.uint8))
        depth_raw = FakeFrame(np.full((2, 3), 2000, dtype=np.uint16))
        depth_aligned = FakeFrame(np.full((2, 3), 1999, dtype=np.uint16))
        ir_left = FakeFrame(np.full((2, 3), 11, dtype=np.uint8))
        ir_right = FakeFrame(np.full((2, 3), 12, dtype=np.uint8))
        self.raw_frames = FakeFrameSet(color, depth_raw, ir_left, ir_right)
        self.aligned_frames = FakeFrameSet(color, depth_aligned)
        self.wait_error = wait_error
        self.created_pipeline = None
        self.created_config = None

    def context(self):
        return FakeContext(self.device)

    def pipeline(self):
        self.created_pipeline = FakePipeline(
            self.device, self.raw_frames, wait_error=self.wait_error
        )
        return self.created_pipeline

    def config(self):
        self.created_config = FakeConfig()
        return self.created_config

    def align(self, target):
        if target != self.stream.color:
            raise RuntimeError("wrong alignment target")
        return FakeAlign(self.aligned_frames)


class CameraAdapterTests(unittest.TestCase):
    def test_orbbec_adapter_prefixes_id_and_does_not_fake_raw_depth(self):
        manager = FakeOrbbecManager()
        adapter = OrbbecCameraAdapter(manager=manager)

        self.assertEqual(adapter.list_devices()[0]["id"], "orbbec:ORB123")
        self.assertTrue(adapter.connect("orbbec:ORB123", width=1280, height=800, fps=30))
        self.assertEqual(manager.connect_arguments["device_id"], "ORB123")
        self.assertEqual(adapter.get_status()["device"]["camera_code"], "C336L")

        bundle = adapter.get_frames()
        self.assertIsNone(bundle.depth_raw)
        np.testing.assert_array_equal(bundle.depth_aligned, manager.depth)
        self.assertIs(bundle.depth, bundle.depth_aligned)
        self.assertFalse(bundle.camera_metadata["depth_raw_available"])
        self.assertEqual(bundle.camera_metadata["timestamp_source"], "camera_manager_software_estimate")
        self.assertEqual(bundle.frame_number, 3)
        self.assertIn("color", bundle.intrinsics)

        adapter.disconnect()
        self.assertTrue(manager.released)

    def test_realsense_missing_sdk_is_reported_without_exception(self):
        adapter = RealSenseCameraAdapter(rs_module=None)

        self.assertEqual(adapter.list_devices(), [])
        self.assertFalse(adapter.connect("realsense:RS123"))
        status = adapter.get_status()
        self.assertFalse(status["sdk_available"])
        self.assertIn("pyrealsense2", status["message"])

    def test_realsense_mock_produces_complete_sdk_independent_bundle(self):
        fake_rs = FakeRealSenseModule()
        adapter = RealSenseCameraAdapter(rs_module=fake_rs)

        devices = adapter.list_devices()
        self.assertEqual(devices[0]["id"], "realsense:RS123")
        self.assertTrue(adapter.connect("realsense:RS123", width=640, height=480, fps=30))
        self.assertEqual(fake_rs.created_config.device_serial, "RS123")
        self.assertEqual(len(fake_rs.created_config.streams), 4)

        bundle = adapter.get_frames(timeout_ms=500)
        self.assertEqual(bundle.depth_scale, 1.0)
        np.testing.assert_array_equal(bundle.depth_raw, np.full((2, 3), 2000, dtype=np.uint16))
        np.testing.assert_array_equal(bundle.depth_aligned, np.full((2, 3), 1999, dtype=np.uint16))
        self.assertEqual(set(bundle.infrared), {"left", "right"})
        self.assertEqual(bundle.device_timestamp, 1234.5)
        self.assertEqual(bundle.frame_number, 42)
        self.assertEqual(
            bundle.camera_metadata["primary_clock"],
            {"timestamp_source": "color", "frame_number_source": "color"},
        )
        self.assertIn("depth_raw", bundle.intrinsics)
        self.assertIn("depth_raw_to_color", bundle.extrinsics)
        self.assertTrue(bundle.camera_metadata["depth_raw_available"])
        self.assertTrue(bundle.camera_metadata["depth_aligned_available"])

        original = fake_rs.raw_frames.depth.get_data()
        self.assertFalse(np.shares_memory(bundle.depth_raw, original))

        pipeline = fake_rs.created_pipeline
        adapter.disconnect()
        self.assertTrue(pipeline.stopped)

    def test_realsense_connect_rejects_pipeline_without_first_frameset(self):
        fake_rs = FakeRealSenseModule(
            wait_error=RuntimeError("Frame didn't arrive within 5000")
        )
        adapter = RealSenseCameraAdapter(rs_module=fake_rs)

        self.assertFalse(
            adapter.connect(
                "realsense:RS123",
                width=640,
                height=480,
                fps=30,
                startup_timeout_ms=5000,
            )
        )
        status = adapter.get_status()
        self.assertFalse(status["connected"])
        self.assertIn("首个同步帧验收失败", status["message"])
        self.assertFalse(status["device"]["stream_preflight"]["passed"])
        self.assertTrue(fake_rs.created_pipeline.stopped)

    def test_registry_routes_prefixed_ids(self):
        orbbec = OrbbecCameraAdapter(manager=FakeOrbbecManager())
        realsense = RealSenseCameraAdapter(rs_module=FakeRealSenseModule())
        registry = CameraAdapterRegistry(orbbec=orbbec, realsense=realsense)

        ids = {device["id"] for device in registry.list_devices()}
        self.assertEqual(ids, {"orbbec:ORB123", "realsense:RS123"})
        self.assertIs(registry.for_device("orbbec:ORB123"), orbbec)
        self.assertIs(registry.for_device("realsense:RS123"), realsense)
        with self.assertRaises(ValueError):
            registry.for_device("RS123")


if __name__ == "__main__":
    unittest.main()
