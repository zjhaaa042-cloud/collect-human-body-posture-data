import json
import cv2
import numpy as np
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any
from loguru import logger

try:
    from pyorbbecsdk import (
        Pipeline, Config, Context, AlignFilter, PointCloudFilter,
        OBSensorType, OBFormat, OBStreamType, OBError,
        OBFrameAggregateOutputMode, OBPropertyID,
    )
    HAS_ORBBEC = True
except ImportError:
    OBError = Exception
    HAS_ORBBEC = False
    logger.warning("pyorbbecsdk not found, using mock mode. Install with: pip install pyorbbecsdk2")

if HAS_ORBBEC:
    CAMERA_PARAMS_MAP = {
        "color_auto_exposure": (OBPropertyID.OB_PROP_COLOR_AUTO_EXPOSURE_BOOL, bool),
        "color_auto_white_balance": (OBPropertyID.OB_PROP_COLOR_AUTO_WHITE_BALANCE_BOOL, bool),
        "color_brightness": (OBPropertyID.OB_PROP_COLOR_BRIGHTNESS_INT, int),
        "color_contrast": (OBPropertyID.OB_PROP_COLOR_CONTRAST_INT, int),
        "color_exposure_time": (OBPropertyID.OB_PROP_COLOR_EXPOSURE_INT, int),
        "color_gain": (OBPropertyID.OB_PROP_COLOR_GAIN_INT, int),
        "color_gamma": (OBPropertyID.OB_PROP_COLOR_GAMMA_INT, int),
        "color_hue": (OBPropertyID.OB_PROP_COLOR_HUE_INT, int),
        "color_saturation": (OBPropertyID.OB_PROP_COLOR_SATURATION_INT, int),
        "color_sharpness": (OBPropertyID.OB_PROP_COLOR_SHARPNESS_INT, int),
        "color_white_balance": (OBPropertyID.OB_PROP_COLOR_WHITE_BALANCE_INT, int),
        "depth_auto_exposure": (OBPropertyID.OB_PROP_DEPTH_AUTO_EXPOSURE_BOOL, bool),
        "depth_exposure_time": (OBPropertyID.OB_PROP_DEPTH_EXPOSURE_INT, int),
        "depth_gain": (OBPropertyID.OB_PROP_DEPTH_GAIN_INT, int),
        "laser_power_level": (OBPropertyID.OB_PROP_LASER_POWER_LEVEL_CONTROL_INT, int),
        "laser_state": (OBPropertyID.OB_PROP_LASER_BOOL, bool),
    }
else:
    CAMERA_PARAMS_MAP = {}


@dataclass
class FrameData:
    color: Optional[np.ndarray]
    depth: Optional[np.ndarray]
    depth_scale: float
    timestamp: int
    frame_number: int


@dataclass
class PointCloudData:
    points: np.ndarray
    colors: Optional[np.ndarray]
    point_count: int
    pixel_indices: Optional[np.ndarray] = None


@dataclass
class CameraIntrinsics:
    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int


class CameraManager:
    def __init__(self):
        self.pipeline = None
        self.config = None
        self.align_filter = None
        self.point_cloud_filter = None
        self.is_initialized = False
        self.is_streaming = False
        self.frame_count = 0

    @staticmethod
    def _frame_format(frame):
        try:
            return frame.get_format()
        except Exception:
            return None

    @staticmethod
    def _frame_data(frame) -> np.ndarray:
        return np.frombuffer(frame.get_data(), dtype=np.uint8)

    def _decode_color_frame(self, color_frame) -> Optional[np.ndarray]:
        try:
            width = color_frame.get_width()
            height = color_frame.get_height()
            frame_format = self._frame_format(color_frame)
            data = self._frame_data(color_frame)
            size = data.size

            if frame_format == OBFormat.MJPG or size < width * height:
                decoded = cv2.imdecode(data, cv2.IMREAD_COLOR)
                if decoded is None:
                    logger.warning(f"Failed to decode compressed color frame: format={frame_format}, bytes={size}")
                    return None
                return cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB)

            if frame_format == OBFormat.RGB:
                return data[:width * height * 3].reshape((height, width, 3))

            if frame_format == OBFormat.BGR:
                bgr = data[:width * height * 3].reshape((height, width, 3))
                return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

            if frame_format == OBFormat.RGBA:
                rgba = data[:width * height * 4].reshape((height, width, 4))
                return cv2.cvtColor(rgba, cv2.COLOR_RGBA2RGB)

            if frame_format == OBFormat.BGRA:
                bgra = data[:width * height * 4].reshape((height, width, 4))
                return cv2.cvtColor(bgra, cv2.COLOR_BGRA2RGB)

            if frame_format in (OBFormat.YUYV, OBFormat.YUY2):
                yuyv = data[:width * height * 2].reshape((height, width, 2))
                return cv2.cvtColor(yuyv, cv2.COLOR_YUV2RGB_YUY2)

            if frame_format == OBFormat.UYVY:
                uyvy = data[:width * height * 2].reshape((height, width, 2))
                return cv2.cvtColor(uyvy, cv2.COLOR_YUV2RGB_UYVY)

            if frame_format == OBFormat.NV12:
                nv12 = data[:width * height * 3 // 2].reshape((height * 3 // 2, width))
                return cv2.cvtColor(nv12, cv2.COLOR_YUV2RGB_NV12)

            if frame_format == OBFormat.NV21:
                nv21 = data[:width * height * 3 // 2].reshape((height * 3 // 2, width))
                return cv2.cvtColor(nv21, cv2.COLOR_YUV2RGB_NV21)

            if frame_format == OBFormat.I420:
                i420 = data[:width * height * 3 // 2].reshape((height * 3 // 2, width))
                return cv2.cvtColor(i420, cv2.COLOR_YUV2RGB_I420)

            if frame_format == OBFormat.YV12:
                yv12 = data[:width * height * 3 // 2].reshape((height * 3 // 2, width))
                return cv2.cvtColor(yv12, cv2.COLOR_YUV2RGB_YV12)

            if frame_format == OBFormat.Y8 or size == width * height:
                gray = data[:width * height].reshape((height, width))
                return cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)

            if size == width * height * 3:
                logger.warning(f"Unknown color frame format {frame_format}; treating data as RGB")
                return data[:width * height * 3].reshape((height, width, 3))

            logger.warning(f"Unsupported color frame format: {frame_format}, bytes={size}, size={width}x{height}")
            return None
        except Exception as e:
            logger.error(f"Failed to decode color frame: {e}")
            return None

    def _decode_depth_frame(self, depth_frame) -> Optional[np.ndarray]:
        try:
            width = depth_frame.get_width()
            height = depth_frame.get_height()
            frame_format = self._frame_format(depth_frame)
            data = self._frame_data(depth_frame)
            expected_u16 = width * height * 2

            if frame_format in (OBFormat.Y16, OBFormat.Z16, OBFormat.RW16) or data.size >= expected_u16:
                depth = np.frombuffer(data[:expected_u16].tobytes(), dtype=np.uint16)
                return depth.reshape((height, width))

            if frame_format == OBFormat.Y8 or data.size >= width * height:
                depth8 = data[:width * height].reshape((height, width))
                return depth8.astype(np.uint16)

            logger.warning(f"Unsupported depth frame format: {frame_format}, bytes={data.size}, size={width}x{height}")
            return None
        except Exception as e:
            logger.error(f"Failed to decode depth frame: {e}")
            return None

    def load_camera_params(self, params_file: str) -> bool:
        try:
            params_path = Path(params_file)
            if not params_path.exists():
                logger.warning(f"Camera params file not found: {params_file}")
                return False

            with open(params_path, 'r') as f:
                params = json.load(f)

            if not HAS_ORBBEC or not self.pipeline:
                logger.warning("Cannot apply camera params: no device connected")
                return False

            device = self.pipeline.get_device()
            if not device:
                logger.error("No device found")
                return False

            for key, value in params.items():
                if key in CAMERA_PARAMS_MAP:
                    prop_id, prop_type = CAMERA_PARAMS_MAP[key]
                    try:
                        if prop_type == bool:
                            device.set_bool_property(prop_id, bool(value))
                        elif prop_type == int:
                            device.set_int_property(prop_id, int(value))
                        logger.debug(f"Set {key} = {value}")
                    except Exception as e:
                        logger.warning(f"Failed to set {key}: {e}")

            logger.info(f"Camera params loaded from {params_file}")
            return True
        except Exception as e:
            logger.error(f"Failed to load camera params: {e}")
            return False

    def initialize(self, width: int = 1280, height: int = 800, fps: int = 30, params_file: str = None) -> bool:
        try:
            if not HAS_ORBBEC:
                logger.warning("Running in mock mode")
                self.is_initialized = True
                return True

            ctx = Context()
            device_list = ctx.query_devices()
            if device_list.get_count() == 0:
                logger.warning("No Orbbec device found, using mock mode")
                self.pipeline = None
                self.is_initialized = True
                return True

            self.pipeline = Pipeline()
            self.config = Config()

            color_profiles = self.pipeline.get_stream_profile_list(OBSensorType.COLOR_SENSOR)
            if color_profiles:
                color_profile = color_profiles.get_video_stream_profile(width, height, OBFormat.RGB, fps)
                self.config.enable_stream(color_profile)

            depth_profiles = self.pipeline.get_stream_profile_list(OBSensorType.DEPTH_SENSOR)
            if depth_profiles:
                try:
                    depth_profile = depth_profiles.get_video_stream_profile(width, height, OBFormat.Y16, fps)
                except Exception:
                    depth_profile = depth_profiles.get_default_video_stream_profile()
                    logger.warning(f"Depth sensor does not support {width}x{height}, using default profile")
                self.config.enable_stream(depth_profile)

            self.config.set_frame_aggregate_output_mode(OBFrameAggregateOutputMode.FULL_FRAME_REQUIRE)

            self.align_filter = AlignFilter(align_to_stream=OBStreamType.COLOR_STREAM)

            self.point_cloud_filter = PointCloudFilter()
            self.point_cloud_filter.set_create_point_format(OBFormat.RGB_POINT)

            if params_file:
                self.load_camera_params(params_file)

            self.is_initialized = True
            logger.info(f"Camera initialized: {width}x{height}@{fps}fps")
            return True
        except OBError as e:
            logger.error(f"Failed to initialize camera: {e}")
            return False

    def start_stream(self) -> bool:
        try:
            if not self.is_initialized:
                logger.error("Camera not initialized")
                return False

            if HAS_ORBBEC and self.pipeline:
                self.pipeline.enable_frame_sync()
                self.pipeline.start(self.config)

            self.is_streaming = True
            self.frame_count = 0
            logger.info("Camera stream started")
            return True
        except OBError as e:
            logger.error(f"Failed to start stream: {e}")
            return False

    def stop_stream(self):
        try:
            if HAS_ORBBEC and self.pipeline:
                self.pipeline.stop()
            self.is_streaming = False
            logger.info("Camera stream stopped")
        except OBError as e:
            logger.error(f"Failed to stop stream: {e}")

    def get_frames(self) -> Optional[FrameData]:
        try:
            if not self.is_streaming:
                return None

            if HAS_ORBBEC and self.pipeline:
                frames = self.pipeline.wait_for_frames(1000)
                if not frames:
                    return None

                color_frame = frames.get_color_frame()
                depth_frame = frames.get_depth_frame()

                if not color_frame or not depth_frame:
                    return None

                aligned_frames = self.align_filter.process(frames)
                if aligned_frames:
                    color_frame = aligned_frames.get_color_frame()
                    depth_frame = aligned_frames.get_depth_frame()

                color_data = None
                if color_frame:
                    if self.frame_count == 0:
                        logger.info(
                            f"Color frame: format={self._frame_format(color_frame)}, "
                            f"size={color_frame.get_width()}x{color_frame.get_height()}, "
                            f"bytes={self._frame_data(color_frame).size}"
                        )
                    color_data = self._decode_color_frame(color_frame)

                depth_data = None
                depth_scale = 1.0
                if depth_frame:
                    raw_scale = depth_frame.get_depth_scale()
                    if raw_scale > 0:
                        depth_scale = raw_scale
                    if self.frame_count == 0:
                        logger.info(
                            f"Depth frame: format={self._frame_format(depth_frame)}, "
                            f"size={depth_frame.get_width()}x{depth_frame.get_height()}, "
                            f"bytes={self._frame_data(depth_frame).size}"
                        )
                    depth_data = self._decode_depth_frame(depth_frame)
                    if depth_data is not None and self.frame_count == 0:
                        valid = depth_data[depth_data > 0]
                        logger.info(f"Depth scale: raw={raw_scale}, used={depth_scale}, raw_range={depth_data.min()}-{depth_data.max()}, valid_count={len(valid)}")
                        if len(valid) > 0:
                            logger.info(f"  mm_range: {valid.min() * depth_scale:.0f}-{valid.max() * depth_scale:.0f}mm")

                self.frame_count += 1
                return FrameData(
                    color=color_data,
                    depth=depth_data,
                    depth_scale=depth_scale,
                    timestamp=self.frame_count * 33,
                    frame_number=self.frame_count
                )
            else:
                self.frame_count += 1
                return FrameData(
                    color=np.random.randint(0, 255, (800, 1280, 3), dtype=np.uint8),
                    depth=np.random.randint(0, 4000, (800, 1280), dtype=np.uint16),
                    depth_scale=1.0,
                    timestamp=self.frame_count * 33,
                    frame_number=self.frame_count
                )
        except Exception as e:
            logger.error(f"Failed to get frames: {e}")
            return None

    def generate_point_cloud(self, frame_data: FrameData, colored: bool = True, stride: int = 1) -> Optional[PointCloudData]:
        try:
            if not frame_data or frame_data.depth is None:
                return None

            height, width = frame_data.depth.shape
            depth_scale = frame_data.depth_scale
            stride = max(1, int(stride or 1))

            intrinsics = self.get_camera_intrinsics()
            fx = intrinsics.fx if intrinsics else width / 2
            fy = intrinsics.fy if intrinsics else width / 2
            cx = intrinsics.cx if intrinsics else width / 2
            cy = intrinsics.cy if intrinsics else height / 2

            v, u = np.mgrid[0:height:stride, 0:width:stride]
            sampled_depth = frame_data.depth[::stride, ::stride]
            z = sampled_depth.astype(np.float32) * depth_scale
            x = (u - cx) * z / fx
            y = (v - cy) * z / fy

            points = np.stack([x, y, z], axis=-1).reshape(-1, 3)
            valid_mask = z.reshape(-1) > 0
            pixel_indices = (v * width + u).reshape(-1)
            valid_indices = pixel_indices[valid_mask]
            points = points[valid_mask]

            colors = None
            if colored and frame_data.color is not None:
                colors = frame_data.color[::stride, ::stride].reshape(-1, 3)[valid_mask]

            return PointCloudData(
                points=points,
                colors=colors,
                point_count=len(points),
                pixel_indices=valid_indices
            )
        except Exception as e:
            logger.error(f"Failed to generate point cloud: {e}")
            return None

    def get_center_distance(self) -> float:
        try:
            if HAS_ORBBEC and self.pipeline:
                frames = self.pipeline.wait_for_frames(1000)
                if not frames:
                    return 0.0
                depth_frame = frames.get_depth_frame()
                if not depth_frame:
                    return 0.0
                scale = depth_frame.get_depth_scale()
                data = self._decode_depth_frame(depth_frame)
                if data is None:
                    return 0.0
                h, w = data.shape
                return float(data[h // 2, w // 2]) * scale
            return 0.0
        except Exception as e:
            logger.error(f"Failed to get center distance: {e}")
            return 0.0

    def get_device_info(self) -> Dict[str, Any]:
        try:
            if HAS_ORBBEC:
                ctx = Context()
                device_list = ctx.query_devices()
                if device_list.get_count() > 0:
                    device = device_list.get_device(0)
                    info = device.get_device_info()
                    return {
                        "name": info.get_name(),
                        "serial_number": info.get_sn(),
                        "firmware_version": info.get_firmware_version(),
                        "device_type": str(info.get_device_type())
                    }
            return {"name": "Mock Camera", "serial_number": "MOCK123"}
        except Exception as e:
            logger.error(f"Failed to get device info: {e}")
            return {}

    def get_camera_intrinsics(self) -> Optional[CameraIntrinsics]:
        try:
            if HAS_ORBBEC and self.pipeline:
                param = self.pipeline.get_camera_param()
                intrinsic = param.rgb_intrinsic
                return CameraIntrinsics(
                    fx=intrinsic.fx,
                    fy=intrinsic.fy,
                    cx=intrinsic.cx,
                    cy=intrinsic.cy,
                    width=intrinsic.width,
                    height=intrinsic.height
                )
            return CameraIntrinsics(fx=500, fy=500, cx=640, cy=400, width=1280, height=800)
        except Exception as e:
            logger.error(f"Failed to get camera intrinsics: {e}")
            return None

    def release(self):
        try:
            if HAS_ORBBEC and self.pipeline:
                self.pipeline.stop()
            self.is_initialized = False
            self.is_streaming = False
            logger.info("Camera released")
        except Exception as e:
            logger.error(f"Failed to release camera: {e}")

    def __del__(self):
        self.release()
