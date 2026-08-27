"""SDK-independent RGB-D camera adapters.

The existing :class:`CameraManager` remains the Orbbec implementation used by
the application.  This module wraps it behind the same small interface as the
Intel RealSense implementation so protocol/storage code does not need to know
which SDK produced a frame.

``FrameBundle.depth_scale`` is always millimetres per raw depth unit.  This is
important because Orbbec reports millimetres per unit while librealsense
reports metres per unit.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from importlib import metadata as importlib_metadata
from typing import Any, Dict, Mapping, Optional, Tuple

import numpy as np
from loguru import logger

from .camera_manager import CameraManager

try:
    import pyrealsense2 as _pyrealsense2

    HAS_REALSENSE = True
except ImportError:
    _pyrealsense2 = None
    HAS_REALSENSE = False


_DEFAULT_RS_MODULE = object()


@dataclass(frozen=True)
class CameraIntrinsicsData:
    """Serializable pinhole camera calibration for one stream."""

    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int
    distortion_model: str = ""
    coefficients: Tuple[float, ...] = ()


@dataclass(frozen=True)
class CameraExtrinsicsData:
    """Rigid transform from ``source`` to ``target`` coordinates."""

    source: str
    target: str
    rotation: Tuple[float, ...]
    translation: Tuple[float, ...]
    translation_unit: str = "meters"


@dataclass
class FrameBundle:
    """One synchronized, SDK-independent RGB-D capture.

    Arrays are owned copies for the RealSense implementation, so callers may
    keep them after the SDK frame object is released.  Infrared streams use
    stable keys such as ``left`` and ``right``.
    """

    color: Optional[np.ndarray] = None
    depth_raw: Optional[np.ndarray] = None
    depth_aligned: Optional[np.ndarray] = None
    infrared: Dict[str, np.ndarray] = field(default_factory=dict)
    depth_scale: float = 1.0
    device_timestamp: Optional[float] = None
    frame_number: Optional[int] = None
    camera_metadata: Dict[str, Any] = field(default_factory=dict)
    intrinsics: Dict[str, CameraIntrinsicsData] = field(default_factory=dict)
    extrinsics: Dict[str, CameraExtrinsicsData] = field(default_factory=dict)
    stream_timestamps: Dict[str, float] = field(default_factory=dict)
    stream_frame_numbers: Dict[str, int] = field(default_factory=dict)
    host_timestamp_ns: int = field(default_factory=time.time_ns)

    @property
    def depth(self) -> Optional[np.ndarray]:
        """Compatibility view: prefer color-aligned depth when available."""

        return self.depth_aligned if self.depth_aligned is not None else self.depth_raw

    @property
    def timestamp(self) -> Optional[float]:
        """Compatibility alias for consumers that used ``FrameData.timestamp``."""

        return self.device_timestamp


class CameraAdapter(ABC):
    """Common camera lifecycle used by protocol-driven capture code."""

    backend: str

    @abstractmethod
    def list_devices(self) -> list[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def connect(
        self,
        device_id: str = "",
        width: Optional[int] = None,
        height: Optional[int] = None,
        fps: int = 30,
        **kwargs: Any,
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    def disconnect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_frames(self, timeout_ms: int = 1000) -> Optional[FrameBundle]:
        raise NotImplementedError

    @abstractmethod
    def get_status(
        self,
        devices: Optional[list[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def release(self) -> None:
        self.disconnect()


def _package_version(package_name: str) -> str:
    try:
        return importlib_metadata.version(package_name)
    except importlib_metadata.PackageNotFoundError:
        return ""


def _prefixed_device_id(backend: str, raw_id: Any) -> str:
    value = str(raw_id if raw_id is not None else "").strip()
    prefix = f"{backend}:"
    return value if value.startswith(prefix) else f"{prefix}{value}"


def _strip_device_id(device_id: str, backend: str) -> str:
    value = str(device_id or "").strip()
    prefix = f"{backend}:"
    if not value:
        return ""
    if ":" in value and not value.startswith(prefix):
        raise ValueError(f"设备 {value} 不属于 {backend} 后端")
    return value[len(prefix) :] if value.startswith(prefix) else value


def _safe_call(obj: Any, method_name: str, default: Any = None, *args: Any) -> Any:
    try:
        method = getattr(obj, method_name)
        return method(*args)
    except Exception:
        return default


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class OrbbecCameraAdapter(CameraAdapter):
    """Compatibility adapter around the project's existing CameraManager."""

    backend = "orbbec"

    def __init__(self, manager: Optional[CameraManager] = None):
        self.manager = manager or CameraManager()
        self.last_error = ""
        self._device: Dict[str, Any] = {}

    def _normalize_devices(self, sources: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        devices = []
        for index, source in enumerate(sources):
            item = dict(source)
            raw_id = item.get("id") or item.get("serial_number") or item.get("uid") or index
            model_text = f"{item.get('name', '')} {item.get('model', '')}".upper()
            camera_code = "C336L" if "GEMINI 336L" in model_text else ""
            item.update(
                {
                    "id": _prefixed_device_id(self.backend, raw_id),
                    "backend": self.backend,
                    "sdk": "pyorbbecsdk",
                    "sdk_available": True,
                    "camera_code": camera_code,
                    "protocol_model_verified": bool(camera_code),
                }
            )
            devices.append(item)
        return devices

    def list_devices(self) -> list[Dict[str, Any]]:
        return self._normalize_devices(self.manager.list_devices())

    def connect(
        self,
        device_id: str = "",
        width: Optional[int] = None,
        height: Optional[int] = None,
        fps: int = 30,
        **kwargs: Any,
    ) -> bool:
        try:
            raw_id = _strip_device_id(device_id, self.backend)
        except ValueError as exc:
            self.last_error = str(exc)
            return False

        params_file = kwargs.get("params_file")
        connected = self.manager.connect(
            width=int(width or 1280),
            height=int(height or 800),
            fps=int(fps),
            params_file=params_file,
            device_id=raw_id,
            enable_infrared=bool(kwargs.get("enable_infrared", True)),
        )
        self.last_error = "" if connected else str(getattr(self.manager, "last_error", "连接 Orbbec 相机失败"))
        source_info = dict(self.manager.get_device_info() or {})
        source_raw_id = source_info.get("id") or source_info.get("serial_number") or source_info.get("uid") or raw_id
        if source_info or source_raw_id:
            source_info["id"] = _prefixed_device_id(self.backend, source_raw_id)
            source_info["backend"] = self.backend
            model_text = f"{source_info.get('name', '')} {source_info.get('model', '')}".upper()
            source_info["camera_code"] = (
                "C336L" if "GEMINI 336L" in model_text else ""
            )
            source_info["protocol_model_verified"] = bool(
                source_info["camera_code"]
            )
        self._device = source_info
        return connected

    def disconnect(self) -> None:
        self.manager.release()

    @staticmethod
    def _intrinsics_from_object(intrinsic: Any, distortion: Any = None) -> Optional[CameraIntrinsicsData]:
        if intrinsic is None:
            return None
        try:
            coefficients: Tuple[float, ...] = ()
            if distortion is not None:
                coefficients = tuple(
                    float(getattr(distortion, name))
                    for name in ("k1", "k2", "p1", "p2", "k3", "k4", "k5", "k6")
                    if hasattr(distortion, name)
                )
            return CameraIntrinsicsData(
                fx=float(intrinsic.fx),
                fy=float(intrinsic.fy),
                cx=float(intrinsic.cx),
                cy=float(intrinsic.cy),
                width=int(intrinsic.width),
                height=int(intrinsic.height),
                distortion_model="orbbec",
                coefficients=coefficients,
            )
        except Exception:
            return None

    def _calibration(self) -> tuple[Dict[str, CameraIntrinsicsData], Dict[str, CameraExtrinsicsData]]:
        intrinsics: Dict[str, CameraIntrinsicsData] = {}
        extrinsics: Dict[str, CameraExtrinsicsData] = {}
        pipeline = getattr(self.manager, "pipeline", None)
        camera_param = _safe_call(pipeline, "get_camera_param") if pipeline is not None else None

        if camera_param is not None:
            color = self._intrinsics_from_object(
                getattr(camera_param, "rgb_intrinsic", None),
                getattr(camera_param, "rgb_distortion", None),
            )
            depth = self._intrinsics_from_object(
                getattr(camera_param, "depth_intrinsic", None),
                getattr(camera_param, "depth_distortion", None),
            )
            if color:
                intrinsics["color"] = color
                intrinsics["depth_aligned"] = color
            if depth:
                intrinsics["depth_raw"] = depth

            transform = getattr(camera_param, "transform", None)
            rotation = np.asarray(getattr(transform, "rot", []), dtype=np.float64).reshape(-1)
            translation = np.asarray(getattr(transform, "transform", []), dtype=np.float64).reshape(-1)
            if rotation.size == 9 and translation.size >= 3:
                # Orbbec's OBExtrinsic translation is documented in millimetres.
                extrinsics["depth_raw_to_color"] = CameraExtrinsicsData(
                    source="depth_raw",
                    target="color",
                    rotation=tuple(float(value) for value in rotation),
                    translation=tuple(float(value) for value in translation[:3]),
                    translation_unit="millimeters",
                )

        if "color" not in intrinsics:
            legacy_intrinsic = self.manager.get_camera_intrinsics()
            converted = self._intrinsics_from_object(legacy_intrinsic)
            if converted:
                intrinsics["color"] = converted
                intrinsics["depth_aligned"] = converted
        return intrinsics, extrinsics

    def get_frames(self, timeout_ms: int = 1000) -> Optional[FrameBundle]:
        del timeout_ms  # CameraManager currently owns its fixed SDK timeout.
        frame = self.manager.get_frames()
        if frame is None:
            return None

        intrinsics, extrinsics = self._calibration()
        depth_raw = getattr(frame, "depth_raw", None)
        depth_aligned = getattr(frame, "depth", None)
        infrared = dict(getattr(frame, "infrared", {}) or {})
        stream_timestamps = dict(getattr(frame, "stream_timestamps", {}) or {})
        has_native_stream_timestamps = bool(stream_timestamps)
        stream_frame_numbers = dict(
            getattr(frame, "stream_frame_numbers", {}) or {}
        )
        if not stream_timestamps:
            stream_timestamps = {
                name: float(frame.timestamp)
                for name, present in (
                    ("color", frame.color is not None),
                    ("depth_aligned", depth_aligned is not None),
                )
                if present
            }
        if not stream_frame_numbers:
            stream_frame_numbers = {
                name: int(frame.frame_number)
                for name, present in (
                    ("color", frame.color is not None),
                    ("depth_aligned", depth_aligned is not None),
                )
                if present
            }
        metadata = {
            "backend": self.backend,
            "sdk": "pyorbbecsdk",
            "sdk_version": _package_version("pyorbbecsdk2"),
            "device": self._device or dict(self.manager.get_device_info() or {}),
            "depth_scale_unit": "millimeters_per_unit",
            "timestamp_unit": "milliseconds",
            "depth_raw_available": depth_raw is not None,
            "depth_aligned_available": depth_aligned is not None,
            "infrared_available": sorted(infrared),
            "timestamp_source": (
                "device_stream_timestamp"
                if has_native_stream_timestamps
                else "camera_manager_software_estimate"
            ),
            "enabled_ir_streams": list(
                getattr(self.manager, "enabled_ir_streams", []) or []
            ),
            "stream_profiles": dict(
                getattr(self.manager, "active_stream_profiles", {}) or {}
            ),
            "runtime_controls": (
                self.manager.get_runtime_control_snapshot()
                if hasattr(self.manager, "get_runtime_control_snapshot")
                else {}
            ),
        }
        if depth_raw is None or not infrared:
            missing = []
            if depth_raw is None:
                missing.append("raw depth")
            if not infrared:
                missing.append("IR")
            metadata["compatibility_note"] = (
                f"当前 Orbbec 帧未提供 {'、'.join(missing)}；适配器不会伪造缺失模态。"
            )
        return FrameBundle(
            color=frame.color,
            depth_raw=depth_raw,
            depth_aligned=depth_aligned,
            infrared=infrared,
            depth_scale=float(frame.depth_scale),
            device_timestamp=_safe_float(frame.timestamp),
            frame_number=_safe_int(frame.frame_number),
            camera_metadata=metadata,
            intrinsics=intrinsics,
            extrinsics=extrinsics,
            stream_timestamps=stream_timestamps,
            stream_frame_numbers=stream_frame_numbers,
        )

    def get_status(
        self,
        devices: Optional[list[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        if devices is None:
            status = dict(self.manager.get_status())
            devices = self._normalize_devices(status.get("devices") or [])
        else:
            devices = list(devices)
            status = dict(self.manager.get_status(devices=devices))
        device = {
            **dict(status.get("device") or {}),
            **dict(self._device or {}),
        }
        if device:
            raw_id = device.get("id") or device.get("serial_number") or device.get("uid") or ""
            device["id"] = _prefixed_device_id(self.backend, raw_id)
            device["backend"] = self.backend
            if not device.get("camera_code"):
                model_text = f"{device.get('name', '')} {device.get('model', '')}".upper()
                device["camera_code"] = (
                    "C336L" if "GEMINI 336L" in model_text else ""
                )
            device["protocol_model_verified"] = bool(device.get("camera_code"))
        status.update(
            {
                "backend": self.backend,
                "device": device,
                "devices": devices,
                "message": self.last_error or status.get("message", ""),
            }
        )
        return status


class RealSenseCameraAdapter(CameraAdapter):
    """Intel RealSense adapter with optional ``pyrealsense2`` dependency."""

    backend = "realsense"

    def __init__(self, rs_module: Any = _DEFAULT_RS_MODULE):
        self.rs = _pyrealsense2 if rs_module is _DEFAULT_RS_MODULE else rs_module
        self.pipeline = None
        self.config = None
        self.align = None
        self.profile = None
        self.connected = False
        self.last_error = ""
        self._device: Dict[str, Any] = {}
        self._depth_scale_mm = 1.0

    @property
    def sdk_available(self) -> bool:
        return self.rs is not None

    def _device_info_value(self, device: Any, key_name: str) -> str:
        if self.rs is None:
            return ""
        key = getattr(getattr(self.rs, "camera_info", None), key_name, None)
        if key is None:
            return ""
        try:
            if hasattr(device, "supports") and not device.supports(key):
                return ""
            return str(device.get_info(key))
        except Exception:
            return ""

    def _describe_device(self, device: Any, index: int) -> Dict[str, Any]:
        serial = self._device_info_value(device, "serial_number")
        raw_id = serial or str(index)
        name = self._device_info_value(device, "name") or f"Intel RealSense {index + 1}"
        camera_code = "CD435I" if "D435I" in name.upper().replace(" ", "") else ""
        return {
            "id": _prefixed_device_id(self.backend, raw_id),
            "backend": self.backend,
            "index": index,
            "name": name,
            "serial_number": serial,
            "firmware_version": self._device_info_value(device, "firmware_version"),
            "product_line": self._device_info_value(device, "product_line"),
            "product_id": self._device_info_value(device, "product_id"),
            "usb_type_descriptor": self._device_info_value(device, "usb_type_descriptor"),
            "sdk": "pyrealsense2",
            "sdk_version": str(
                getattr(self.rs, "__version__", "")
                or _package_version("pyrealsense2")
            ),
            "sdk_available": True,
            "camera_code": camera_code,
            "protocol_model_verified": bool(camera_code),
        }

    def list_devices(self) -> list[Dict[str, Any]]:
        if not self.sdk_available:
            return []
        try:
            context = self.rs.context()
            return [self._describe_device(device, index) for index, device in enumerate(context.query_devices())]
        except Exception as exc:
            self.last_error = f"枚举 RealSense 设备失败: {exc}"
            logger.warning(self.last_error)
            return []

    def connect(
        self,
        device_id: str = "",
        width: Optional[int] = None,
        height: Optional[int] = None,
        fps: int = 30,
        **kwargs: Any,
    ) -> bool:
        self.disconnect()
        if not self.sdk_available:
            self.last_error = "未安装 Intel RealSense SDK（pyrealsense2）"
            logger.warning(self.last_error)
            return False

        try:
            raw_id = _strip_device_id(device_id, self.backend)
        except ValueError as exc:
            self.last_error = str(exc)
            return False

        devices = self.list_devices()
        if not devices:
            self.last_error = self.last_error or "未检测到 Intel RealSense 设备"
            return False

        selected = devices[0]
        if raw_id:
            selected = next(
                (
                    device
                    for device in devices
                    if raw_id in {device.get("serial_number"), str(device.get("index")), device.get("id")}
                ),
                {},
            )
            if not selected:
                self.last_error = f"未找到 RealSense 设备: {device_id}"
                return False

        try:
            stream = self.rs.stream
            pixel_format = self.rs.format
            capture_width = int(width or 1280)
            capture_height = int(height or 720)
            capture_fps = int(fps)
            enable_infrared = bool(kwargs.get("enable_infrared", True))

            self.pipeline = self.rs.pipeline()
            self.config = self.rs.config()
            serial = selected.get("serial_number")
            if serial:
                self.config.enable_device(serial)
            self.config.enable_stream(stream.depth, capture_width, capture_height, pixel_format.z16, capture_fps)
            self.config.enable_stream(stream.color, capture_width, capture_height, pixel_format.rgb8, capture_fps)
            if enable_infrared:
                self.config.enable_stream(stream.infrared, 1, capture_width, capture_height, pixel_format.y8, capture_fps)
                self.config.enable_stream(stream.infrared, 2, capture_width, capture_height, pixel_format.y8, capture_fps)

            self.profile = self.pipeline.start(self.config)
            self.align = self.rs.align(stream.color)
            active_device = self.profile.get_device()
            depth_sensor = active_device.first_depth_sensor()
            scale_meters = float(depth_sensor.get_depth_scale())
            self._depth_scale_mm = scale_meters * 1000.0
            self._device = dict(selected)
            self._device["depth_scale_meters_per_unit"] = scale_meters

            # ``pipeline.start`` only proves that librealsense accepted the
            # requested profiles.  A disconnected UVC endpoint or a failing
            # cable can still leave the pipeline open while every subsequent
            # wait times out.  Do not report that state as a usable camera.
            startup_timeout_ms = max(
                1000,
                min(int(kwargs.get("startup_timeout_ms", 5000)), 30000),
            )
            try:
                startup_frames = self.pipeline.wait_for_frames(startup_timeout_ms)
                startup_aligned = self.align.process(startup_frames)
                required_frames = {
                    "color": _safe_call(startup_frames, "get_color_frame"),
                    "depth_raw": _safe_call(startup_frames, "get_depth_frame"),
                    "depth_aligned": _safe_call(startup_aligned, "get_depth_frame"),
                }
                if enable_infrared:
                    required_frames.update(
                        {
                            "infrared_left": _safe_call(
                                startup_frames, "get_infrared_frame", None, 1
                            ),
                            "infrared_right": _safe_call(
                                startup_frames, "get_infrared_frame", None, 2
                            ),
                        }
                    )
                missing = [
                    name for name, frame in required_frames.items() if not frame
                ]
                if missing:
                    raise RuntimeError(
                        "首个同步帧缺少必需模态: " + ", ".join(missing)
                    )
            except Exception as exc:
                self._device["stream_preflight"] = {
                    "passed": False,
                    "timeout_ms": startup_timeout_ms,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                raise RuntimeError(f"首个同步帧验收失败: {exc}") from exc

            self._device["stream_preflight"] = {
                "passed": True,
                "timeout_ms": startup_timeout_ms,
                "required_modalities": list(required_frames),
            }
            self.connected = True
            self.last_error = ""
            return True
        except Exception as exc:
            self.last_error = f"连接 RealSense 相机失败: {exc}"
            logger.error(self.last_error)
            self.disconnect()
            return False

    def disconnect(self) -> None:
        if self.pipeline is not None:
            try:
                self.pipeline.stop()
            except Exception:
                pass
        self.pipeline = None
        self.config = None
        self.align = None
        self.profile = None
        self.connected = False

    @staticmethod
    def _array_from_frame(frame: Any) -> Optional[np.ndarray]:
        if frame is None:
            return None
        try:
            return np.asanyarray(frame.get_data()).copy()
        except Exception:
            return None

    @staticmethod
    def _intrinsics_from_frame(frame: Any) -> Optional[CameraIntrinsicsData]:
        if frame is None:
            return None
        try:
            video_profile = frame.profile.as_video_stream_profile()
            intrinsic = video_profile.get_intrinsics()
            return CameraIntrinsicsData(
                fx=float(intrinsic.fx),
                fy=float(intrinsic.fy),
                cx=float(intrinsic.ppx),
                cy=float(intrinsic.ppy),
                width=int(intrinsic.width),
                height=int(intrinsic.height),
                distortion_model=str(intrinsic.model),
                coefficients=tuple(float(value) for value in intrinsic.coeffs),
            )
        except Exception:
            return None

    @staticmethod
    def _extrinsics_between(
        source_name: str,
        source_frame: Any,
        target_name: str,
        target_frame: Any,
    ) -> Optional[CameraExtrinsicsData]:
        if source_frame is None or target_frame is None:
            return None
        try:
            source_profile = source_frame.profile.as_video_stream_profile()
            target_profile = target_frame.profile.as_video_stream_profile()
            extrinsic = source_profile.get_extrinsics_to(target_profile)
            return CameraExtrinsicsData(
                source=source_name,
                target=target_name,
                rotation=tuple(float(value) for value in extrinsic.rotation),
                translation=tuple(float(value) for value in extrinsic.translation),
                translation_unit="meters",
            )
        except Exception:
            return None

    @staticmethod
    def _stream_clock(frame: Any) -> tuple[Optional[float], Optional[int]]:
        if frame is None:
            return None, None
        return _safe_float(_safe_call(frame, "get_timestamp")), _safe_int(_safe_call(frame, "get_frame_number"))

    def _frame_metadata(self, frame: Any) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        if frame is None or self.rs is None:
            return result
        metadata_enum = getattr(self.rs, "frame_metadata_value", None)
        if metadata_enum is None:
            return result
        for name in (
            "exposure",
            "gain_level",
            "actual_fps",
            "white_balance",
            "laser_power",
            "backend_timestamp",
            "sensor_timestamp",
        ):
            key = getattr(metadata_enum, name, None)
            if key is None:
                continue
            try:
                if frame.supports_frame_metadata(key):
                    result[name] = frame.get_frame_metadata(key)
            except Exception:
                continue
        return result

    @staticmethod
    def _stream_profile_summary(frame: Any) -> Dict[str, Any]:
        if frame is None:
            return {}
        try:
            profile = frame.profile.as_video_stream_profile()
            return {
                "width": int(profile.width()),
                "height": int(profile.height()),
                "fps": int(profile.fps()),
                "format": str(profile.format()),
            }
        except Exception:
            return {}

    def get_frames(self, timeout_ms: int = 1000) -> Optional[FrameBundle]:
        if not self.connected or self.pipeline is None:
            self.last_error = "RealSense 相机尚未连接"
            return None

        try:
            frames = self.pipeline.wait_for_frames(int(timeout_ms))
            raw_depth_frame = frames.get_depth_frame()
            raw_color_frame = frames.get_color_frame()
            ir_left_frame = _safe_call(frames, "get_infrared_frame", None, 1)
            ir_right_frame = _safe_call(frames, "get_infrared_frame", None, 2)
            aligned_frames = self.align.process(frames) if self.align is not None else None
            aligned_depth_frame = aligned_frames.get_depth_frame() if aligned_frames is not None else None
            color_frame = aligned_frames.get_color_frame() if aligned_frames is not None else raw_color_frame

            color = self._array_from_frame(color_frame)
            depth_raw = self._array_from_frame(raw_depth_frame)
            depth_aligned = self._array_from_frame(aligned_depth_frame)
            infrared = {
                name: array
                for name, array in (
                    ("left", self._array_from_frame(ir_left_frame)),
                    ("right", self._array_from_frame(ir_right_frame)),
                )
                if array is not None
            }
            if color is None and depth_raw is None and depth_aligned is None and not infrared:
                self.last_error = "RealSense 帧集中没有可用图像"
                return None

            named_frames = {
                "color": color_frame,
                "depth_raw": raw_depth_frame,
                "depth_aligned": aligned_depth_frame,
                "infrared_left": ir_left_frame,
                "infrared_right": ir_right_frame,
            }
            intrinsics = {
                name: intrinsic
                for name, frame in named_frames.items()
                if (intrinsic := self._intrinsics_from_frame(frame)) is not None
            }
            extrinsics: Dict[str, CameraExtrinsicsData] = {}
            for name, source_frame, target_name, target_frame in (
                ("depth_raw_to_color", raw_depth_frame, "color", color_frame),
                ("color_to_depth_raw", color_frame, "depth_raw", raw_depth_frame),
                ("infrared_left_to_color", ir_left_frame, "color", color_frame),
                ("infrared_right_to_color", ir_right_frame, "color", color_frame),
            ):
                source_name = name[: -len(f"_to_{target_name}")]
                extrinsic = self._extrinsics_between(source_name, source_frame, target_name, target_frame)
                if extrinsic is not None:
                    extrinsics[name] = extrinsic

            stream_timestamps: Dict[str, float] = {}
            stream_frame_numbers: Dict[str, int] = {}
            for name, frame in named_frames.items():
                timestamp, frame_number = self._stream_clock(frame)
                if timestamp is not None:
                    stream_timestamps[name] = timestamp
                if frame_number is not None:
                    stream_frame_numbers[name] = frame_number

            # A RealSense ``composite_frame`` does not guarantee that its
            # timestamp/frame number always comes from the same embedded
            # stream.  Using it as the primary clock can therefore create
            # artificial zeroes or backwards jumps even while every video
            # stream is monotonic.  The protocol is aligned to the color
            # coordinate system, so use color as the explicit primary clock,
            # then raw depth, and keep the composite value as a final fallback.
            device_timestamp = stream_timestamps.get("color")
            timestamp_source = "color"
            if device_timestamp is None:
                device_timestamp = stream_timestamps.get("depth_raw")
                timestamp_source = "depth_raw"
            if device_timestamp is None:
                device_timestamp = _safe_float(_safe_call(frames, "get_timestamp"))
                timestamp_source = "composite_frame"

            frame_number = stream_frame_numbers.get("color")
            frame_number_source = "color"
            if frame_number is None:
                frame_number = stream_frame_numbers.get("depth_raw")
                frame_number_source = "depth_raw"
            if frame_number is None:
                frame_number = _safe_int(_safe_call(frames, "get_frame_number"))
                frame_number_source = "composite_frame"

            metadata = {
                "backend": self.backend,
                "sdk": "pyrealsense2",
                "sdk_version": str(
                    getattr(self.rs, "__version__", "")
                    or _package_version("pyrealsense2")
                ),
                "device": dict(self._device),
                "depth_scale_unit": "millimeters_per_unit",
                "timestamp_unit": "milliseconds",
                "primary_clock": {
                    "timestamp_source": timestamp_source,
                    "frame_number_source": frame_number_source,
                },
                "depth_raw_available": depth_raw is not None,
                "depth_aligned_available": depth_aligned is not None,
                "infrared_available": bool(infrared),
                "stream_profiles": {
                    name: summary
                    for name, frame in named_frames.items()
                    if (summary := self._stream_profile_summary(frame))
                },
                "frame_metadata": {
                    name: values
                    for name, frame in named_frames.items()
                    if (values := self._frame_metadata(frame))
                },
            }
            self.last_error = ""
            return FrameBundle(
                color=color,
                depth_raw=depth_raw,
                depth_aligned=depth_aligned,
                infrared=infrared,
                depth_scale=self._depth_scale_mm,
                device_timestamp=device_timestamp,
                frame_number=frame_number,
                camera_metadata=metadata,
                intrinsics=intrinsics,
                extrinsics=extrinsics,
                stream_timestamps=stream_timestamps,
                stream_frame_numbers=stream_frame_numbers,
            )
        except Exception as exc:
            self.last_error = f"获取 RealSense 帧失败: {exc}"
            logger.error(self.last_error)
            return None

    def get_status(
        self,
        devices: Optional[list[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        devices = (
            self.list_devices()
            if devices is None and self.sdk_available
            else list(devices or [])
        )
        if not self.sdk_available:
            message = "未安装 Intel RealSense SDK（pyrealsense2）"
        elif self.connected:
            message = "RealSense 相机已连接"
        elif devices:
            message = "检测到 RealSense 设备，尚未连接"
        else:
            message = "未检测到 Intel RealSense 设备"
        return {
            "backend": self.backend,
            "sdk_available": self.sdk_available,
            "device_present": bool(devices or self._device or self.connected),
            "connected": self.connected,
            "initialized": self.connected,
            "streaming": self.connected,
            "device": dict(self._device),
            "devices": devices,
            "message": self.last_error or message,
        }


class CameraAdapterRegistry:
    """Enumerate both SDKs and create the adapter selected by prefixed ID."""

    def __init__(
        self,
        orbbec: Optional[CameraAdapter] = None,
        realsense: Optional[CameraAdapter] = None,
    ):
        self.adapters: Mapping[str, CameraAdapter] = {
            "orbbec": orbbec or OrbbecCameraAdapter(),
            "realsense": realsense or RealSenseCameraAdapter(),
        }

    def list_devices(self) -> list[Dict[str, Any]]:
        devices: list[Dict[str, Any]] = []
        for adapter in self.adapters.values():
            devices.extend(adapter.list_devices())
        return devices

    def for_device(self, device_id: str) -> CameraAdapter:
        value = str(device_id or "").strip()
        if ":" not in value:
            raise ValueError("设备 ID 必须包含后端前缀，例如 orbbec:SERIAL 或 realsense:SERIAL")
        backend = value.split(":", 1)[0]
        try:
            return self.adapters[backend]
        except KeyError as exc:
            raise ValueError(f"不支持的相机后端: {backend}") from exc


def list_camera_devices() -> list[Dict[str, Any]]:
    """Convenience API for UI/device discovery."""

    return CameraAdapterRegistry().list_devices()


def create_camera_adapter(device_id: str) -> CameraAdapter:
    """Create the correct backend for a prefixed device ID."""

    return CameraAdapterRegistry().for_device(device_id)


__all__ = [
    "CameraAdapter",
    "CameraAdapterRegistry",
    "CameraExtrinsicsData",
    "CameraIntrinsicsData",
    "FrameBundle",
    "HAS_REALSENSE",
    "OrbbecCameraAdapter",
    "RealSenseCameraAdapter",
    "create_camera_adapter",
    "list_camera_devices",
]
