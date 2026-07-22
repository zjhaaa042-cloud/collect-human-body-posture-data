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
    logger.warning("pyorbbecsdk not found. Install with: pip install pyorbbecsdk2")

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
    orientation: str = "landscape"


class CameraManager:
    ORIENTATIONS = {"landscape", "portrait_cw", "portrait_ccw"}

    def __init__(self, orientation: str = "landscape"):
        self.pipeline = None
        self.config = None
        self.align_filter = None
        self.point_cloud_filter = None
        self.is_initialized = False
        self.is_streaming = False
        self.frame_count = 0
        self.last_error = ""
        self.device_info = {}
        self.orientation = self._validate_orientation(orientation)

    @classmethod
    def _validate_orientation(cls, orientation: str) -> str:
        value = str(orientation or "landscape").strip().lower()
        return value if value in cls.ORIENTATIONS else "landscape"

    def set_orientation(self, orientation: str) -> str:
        self.orientation = self._validate_orientation(orientation)
        return self.orientation

    def _rotate_array(self, frame: Optional[np.ndarray]) -> Optional[np.ndarray]:
        if frame is None or self.orientation == "landscape":
            return frame
        rotate_code = cv2.ROTATE_90_CLOCKWISE if self.orientation == "portrait_cw" else cv2.ROTATE_90_COUNTERCLOCKWISE
        return cv2.rotate(frame, rotate_code)

    @staticmethod
    def transform_intrinsics(intrinsics: CameraIntrinsics, orientation: str) -> CameraIntrinsics:
        orientation = CameraManager._validate_orientation(orientation)
        if orientation == "landscape":
            return CameraIntrinsics(**{**intrinsics.__dict__, "orientation": orientation})
        if orientation == "portrait_cw":
            cx = intrinsics.height - 1 - intrinsics.cy
            cy = intrinsics.cx
        else:
            cx = intrinsics.cy
            cy = intrinsics.width - 1 - intrinsics.cx
        return CameraIntrinsics(
            fx=intrinsics.fy,
            fy=intrinsics.fx,
            cx=float(cx),
            cy=float(cy),
            width=intrinsics.height,
            height=intrinsics.width,
            orientation=orientation,
        )

    def get_orientation_metadata(self) -> Dict[str, Any]:
        matrices = {
            "landscape": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
            "portrait_cw": [[0, -1, 0], [1, 0, 0], [0, 0, 1]],
            "portrait_ccw": [[0, 1, 0], [-1, 0, 0], [0, 0, 1]],
        }
        metadata = {"orientation": self.orientation, "raw_to_output_rotation": matrices[self.orientation]}
        raw = self.get_raw_camera_intrinsics()
        if raw:
            output = self.transform_intrinsics(raw, self.orientation)
            metadata.update({
                "raw_resolution": [raw.width, raw.height],
                "output_resolution": [output.width, output.height],
                "raw_intrinsics": raw.__dict__,
                "output_intrinsics": output.__dict__,
            })
        return metadata

    def get_calibration_snapshot(self, depth_scale: float, calibration_version: str = "orbbec_d2c_v1") -> Dict[str, Any]:
        """Build an immutable calibration/configuration snapshot for one capture."""
        metadata = self.get_orientation_metadata()
        return {
            "calibration_version": str(calibration_version or "unknown"),
            "camera": {
                "name": str(self.device_info.get("name", "")),
                "serial_number": str(self.device_info.get("serial_number", "")),
                "uid": str(self.device_info.get("uid", "")),
                "connection_type": str(self.device_info.get("connection_type", "")),
            },
            "orientation": metadata.get("orientation", self.orientation),
            "raw_to_output_rotation": metadata.get("raw_to_output_rotation"),
            "raw_resolution": metadata.get("raw_resolution"),
            "output_resolution": metadata.get("output_resolution"),
            "raw_intrinsics": metadata.get("raw_intrinsics"),
            "output_intrinsics": metadata.get("output_intrinsics"),
            "depth_unit_mm": float(depth_scale),
            "alignment": "depth_to_color",
            "coordinate_unit": "millimeter",
        }

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

    @staticmethod
    def _safe_device_list_value(device_list, method_name: str, index: int):
        try:
            method = getattr(device_list, method_name)
            return method(index)
        except Exception:
            return ""

    def list_devices(self) -> list:
        try:
            if not HAS_ORBBEC:
                return []
            ctx = Context()
            device_list = ctx.query_devices()
            devices = []
            for index in range(device_list.get_count()):
                name = self._safe_device_list_value(device_list, "get_device_name_by_index", index)
                serial_number = self._safe_device_list_value(device_list, "get_device_serial_number_by_index", index)
                uid = self._safe_device_list_value(device_list, "get_device_uid_by_index", index)
                connection_type = self._safe_device_list_value(device_list, "get_device_connection_type_by_index", index)
                device_id = serial_number or uid or str(index)
                devices.append({
                    "id": str(device_id),
                    "index": index,
                    "name": str(name or f"Orbbec Camera {index + 1}"),
                    "serial_number": str(serial_number or ""),
                    "uid": str(uid or ""),
                    "connection_type": str(connection_type or "")
                })
            return devices
        except Exception as e:
            logger.warning(f"Failed to list Orbbec devices: {e}")
            return []

    def _query_first_device_info(self) -> Dict[str, Any]:
        devices = self.list_devices()
        return devices[0] if devices else {}

    def _get_device_by_id(self, device_id: str = ""):
        if not HAS_ORBBEC:
            return None, self._query_first_device_info()
        ctx = Context()
        device_list = ctx.query_devices()
        if device_list.get_count() == 0:
            return None, {}

        device_id = str(device_id or "").strip()
        devices = self.list_devices()
        selected = None
        selected_info = devices[0] if devices else {}

        if device_id:
            for item in devices:
                if device_id in {item.get("id"), item.get("serial_number"), item.get("uid"), str(item.get("index"))}:
                    selected_info = item
                    break

        try:
            serial = selected_info.get("serial_number")
            uid = selected_info.get("uid")
            index = int(selected_info.get("index", 0))
            if serial:
                selected = device_list.get_device_by_serial_number(serial)
            elif uid:
                selected = device_list.get_device_by_uid(uid)
            else:
                selected = device_list.get_device_by_index(index)
        except Exception as e:
            logger.warning(f"Failed to select Orbbec device {device_id}: {e}")
            selected = device_list.get_device_by_index(0)
            selected_info = devices[0] if devices else {}

        return selected, selected_info

    @staticmethod
    def _select_video_profile(profile_list, width: int, height: int, frame_format, fps: int, stream_name: str):
        try:
            return profile_list.get_video_stream_profile(width, height, frame_format, fps)
        except Exception as e:
            logger.warning(
                f"{stream_name} stream does not support {width}x{height}@{fps} {frame_format}, "
                f"using default profile: {e}"
            )
            try:
                return profile_list.get_default_video_stream_profile()
            except Exception as default_error:
                logger.error(f"Failed to get default {stream_name} stream profile: {default_error}")
                return None

    def get_status(self) -> Dict[str, Any]:
        devices = self.list_devices()
        device = self.device_info or (devices[0] if devices else {})
        connected = bool(HAS_ORBBEC and self.pipeline and self.is_initialized and self.is_streaming)
        device_present = bool(devices or device or connected)
        if connected:
            message = "摄像头已连接"
        elif not HAS_ORBBEC:
            message = "未安装 Orbbec 相机 SDK"
        elif device_present:
            message = "检测到设备，尚未连接"
        else:
            message = "未检测到奥比中光设备"
        return {
            "sdk_available": HAS_ORBBEC,
            "device_present": device_present,
            "connected": connected,
            "initialized": self.is_initialized,
            "streaming": self.is_streaming,
            "device": device,
            "devices": devices,
            "message": message if connected else (self.last_error or message)
        }

    def connect(self, width: int = 1280, height: int = 800, fps: int = 30, params_file: str = None, device_id: str = "") -> bool:
        self.release()
        if not self.initialize(width=width, height=height, fps=fps, params_file=params_file, device_id=device_id):
            return False
        return self.start_stream()

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

    def initialize(self, width: int = 1280, height: int = 800, fps: int = 30, params_file: str = None, device_id: str = "") -> bool:
        try:
            self.last_error = ""
            self.device_info = {}
            self.pipeline = None
            self.config = None
            self.align_filter = None
            self.point_cloud_filter = None
            self.is_initialized = False
            self.is_streaming = False

            if not HAS_ORBBEC:
                self.last_error = "未安装 Orbbec 相机 SDK"
                logger.warning(self.last_error)
                return False

            ctx = Context()
            device_list = ctx.query_devices()
            if device_list.get_count() == 0:
                self.last_error = "未检测到奥比中光设备"
                logger.warning(self.last_error)
                return False

            selected_device, selected_info = self._get_device_by_id(device_id)
            if selected_device is None:
                self.last_error = "无法打开选中的奥比中光设备"
                logger.error(self.last_error)
                return False
            self.device_info = selected_info

            self.pipeline = Pipeline(selected_device)
            self.config = Config()
            enabled_streams = 0

            color_profiles = self.pipeline.get_stream_profile_list(OBSensorType.COLOR_SENSOR)
            if color_profiles:
                color_profile = self._select_video_profile(color_profiles, width, height, OBFormat.RGB, fps, "color")
                if color_profile:
                    self.config.enable_stream(color_profile)
                    enabled_streams += 1

            depth_profiles = self.pipeline.get_stream_profile_list(OBSensorType.DEPTH_SENSOR)
            if depth_profiles:
                depth_profile = self._select_video_profile(depth_profiles, width, height, OBFormat.Y16, fps, "depth")
                if depth_profile:
                    self.config.enable_stream(depth_profile)
                    enabled_streams += 1

            if enabled_streams == 0:
                self.last_error = "未找到可用的相机视频流"
                logger.error(self.last_error)
                return False

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
            self.last_error = f"初始化相机失败: {e}"
            logger.error(self.last_error)
            self.release()
            return False
        except Exception as e:
            self.last_error = f"初始化相机失败: {e}"
            logger.error(self.last_error)
            self.release()
            return False

    def start_stream(self) -> bool:
        try:
            if not self.is_initialized:
                self.last_error = "相机尚未初始化"
                logger.error(self.last_error)
                return False

            if not HAS_ORBBEC or not self.pipeline:
                self.last_error = "未连接真实相机"
                logger.error(self.last_error)
                return False

            self.pipeline.enable_frame_sync()
            self.pipeline.start(self.config)

            self.is_streaming = True
            self.frame_count = 0
            self.last_error = ""
            logger.info("Camera stream started")
            return True
        except OBError as e:
            error_text = str(e)
            if "0xc00d3704" in error_text.lower():
                self.last_error = "摄像头正被 OrbbecViewer 或其他程序占用，请关闭占用程序后重新连接"
            else:
                self.last_error = f"启动相机视频流失败: {e}"
            logger.error(self.last_error)
            self.is_streaming = False
            return False
        except Exception as e:
            error_text = str(e)
            if "0xc00d3704" in error_text.lower():
                self.last_error = "摄像头正被 OrbbecViewer 或其他程序占用，请关闭占用程序后重新连接"
            else:
                self.last_error = f"启动相机视频流失败: {e}"
            logger.error(self.last_error)
            self.is_streaming = False
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

            if not HAS_ORBBEC or not self.pipeline:
                return None

            frames = self.pipeline.wait_for_frames(1000)
            if not frames:
                return None

            color_frame = frames.get_color_frame()
            depth_frame = frames.get_depth_frame()

            if not color_frame or not depth_frame:
                return None

            aligned_frames = self.align_filter.process(frames) if self.align_filter else None
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

            if color_data is None and depth_data is None:
                return None

            self.frame_count += 1
            return FrameData(
                color=self._rotate_array(color_data),
                depth=self._rotate_array(depth_data),
                depth_scale=depth_scale,
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
        return self.device_info or self._query_first_device_info()

    def get_raw_camera_intrinsics(self) -> Optional[CameraIntrinsics]:
        try:
            if HAS_ORBBEC and self.pipeline:
                param = self.pipeline.get_camera_param()
                intrinsic = param.rgb_intrinsic
                raw = CameraIntrinsics(
                    fx=intrinsic.fx,
                    fy=intrinsic.fy,
                    cx=intrinsic.cx,
                    cy=intrinsic.cy,
                    width=intrinsic.width,
                    height=intrinsic.height,
                    orientation="landscape",
                )
                return raw
            return None
        except Exception as e:
            logger.error(f"Failed to get camera intrinsics: {e}")
            return None

    def get_camera_intrinsics(self) -> Optional[CameraIntrinsics]:
        raw = self.get_raw_camera_intrinsics()
        return self.transform_intrinsics(raw, self.orientation) if raw else None

    def release(self):
        try:
            if HAS_ORBBEC and self.pipeline:
                try:
                    self.pipeline.stop()
                except Exception:
                    pass
            self.pipeline = None
            self.config = None
            self.align_filter = None
            self.point_cloud_filter = None
            self.is_initialized = False
            self.is_streaming = False
            logger.info("Camera released")
        except Exception as e:
            logger.error(f"Failed to release camera: {e}")

    def __del__(self):
        self.release()
