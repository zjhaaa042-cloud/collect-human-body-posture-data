import asyncio
import json
import re
import secrets
import ipaddress
import time
import os
import websockets
from websockets.http11 import Response
from websockets.datastructures import Headers
from typing import Set, Callable, Any
from urllib.parse import urlparse
from loguru import logger

from ..core.camera_manager import CameraManager
from ..core.depth_analyzer import DepthAnalyzer, DistanceStatus
from ..core.data_collector import DataCollector, CaptureConfig
from ..voice.recognizer import VoiceRecognizer
from ..voice.synthesizer import VoiceSynthesizer
from ..voice.command_parser import VoiceCommandParser, VoiceCommand
from ..utils.frame_processor import FrameProcessor
from ..config.settings import get_settings, save_settings

_ERROR_MESSAGES = {
    json.JSONDecodeError: "请求格式无效",
    KeyError: "缺少必要参数",
    ValueError: "参数值无效",
    PermissionError: "权限不足",
    FileNotFoundError: "资源未找到",
}

_MAX_LENGTHS = {
    "session_name": 100,
    "text": 500,
    "filename": 50,
}

_SAFE_PATTERN = re.compile(r'^[\w\-\u4e00-\u9fff\s]+$')
_FILENAME_PATTERN = re.compile(r'^[\w\-\.]+$')

_ALLOWED_AUTH_ORIGINS = {
    "http://localhost:3000",
    "http://127.0.0.1:3000",
}

_MAX_AUTO_STABLE_FRAMES = 120
_MAX_AUTO_DISTANCE_DELTA_MM = 1000.0
_MAX_AUTO_CAPTURE_COUNT = 100
_MAX_AUTO_CAPTURE_INTERVAL_SEC = 60.0


def _is_local_address(remote_address) -> bool:
    if not remote_address:
        return False
    host = remote_address[0] if isinstance(remote_address, tuple) else remote_address
    host = str(host).strip().lower()
    if host.startswith("::ffff:"):
        host = host[7:]
    return host in {"localhost", "127.0.0.1", "::1"} or host.startswith("127.")


def _is_local_connection(connection) -> bool:
    try:
        return _is_local_address(connection.remote_address)
    except Exception:
        return False


def _is_allowed_auth_origin(origin: str) -> bool:
    if not origin:
        return False
    if origin in _ALLOWED_AUTH_ORIGINS:
        return True
    try:
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or parsed.port != 3000:
            return False
        host = parsed.hostname or ""
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback
    except Exception:
        return False


def _cors_headers_for_origin(origin: str):
    if origin and _is_allowed_auth_origin(origin):
        return [
            ("Access-Control-Allow-Origin", origin),
            ("Vary", "Origin"),
        ]
    return []


def _validate_field(value: str, field_name: str, pattern=None) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    value = value.strip()
    max_len = _MAX_LENGTHS.get(field_name, 200)
    if len(value) > max_len:
        raise ValueError(f"{field_name} exceeds max length {max_len}")
    if pattern and not pattern.match(value):
        raise ValueError(f"{field_name} contains invalid characters")
    return value


class WebSocketServer:
    def __init__(self, host: str = "localhost", port: int = 8765):
        self.host = host
        self.port = port
        self.clients: Set[websockets.WebSocketServerProtocol] = set()
        self.auth_token = secrets.token_urlsafe(32)

        self.settings = get_settings()
        self.camera = CameraManager(self.settings.camera.orientation)
        self.depth_analyzer = DepthAnalyzer(
            target_distance_mm=self.settings.distance.target_distance_mm,
            tolerance_mm=self.settings.distance.tolerance_mm,
            roi_ratio=self.settings.distance.roi_ratio,
            min_distance_mm=self.settings.distance.min_distance_mm,
            max_distance_mm=self.settings.distance.max_distance_mm,
            min_edge_margin=self.settings.distance.min_edge_margin,
            min_body_depth_coverage=self.settings.distance.min_body_depth_coverage,
            min_quality_score=self.settings.distance.min_quality_score,
            pose_model_path=self.settings.distance.pose_model_path,
        )
        self.data_collector = DataCollector(self.settings.storage.output_dir)
        preview_size = self._preview_size_for_orientation(self.camera.orientation)
        self.frame_processor = FrameProcessor(
            preview_size=preview_size,
            jpeg_quality=self.settings.gui.jpeg_quality
        )

        self.voice_recognizer = None
        self.voice_synthesizer = None
        self.voice_parser = VoiceCommandParser()
        self.loop = None  # Store reference to main event loop

        self.is_previewing = False
        self.is_capturing = False
        self.capture_cancelled = False
        self.is_shutting_down = False
        self.capture_lock = asyncio.Lock()
        self.camera_lock = asyncio.Lock()
        self.preview_task = None
        self._last_color_preview = ""
        self._last_depth_preview = ""
        self._preview_miss_count = 0
        self.auto_capture_enabled = False
        self.auto_capture_options = {}
        self.auto_required_frames = 10
        self.auto_max_distance_delta_mm = 30.0
        self.auto_target_count = 3
        self.auto_capture_interval_sec = 1.0
        self.auto_stable_distances = []
        self.auto_captured_count = 0
        self.auto_state = "idle"
        self.auto_message = "自动采集未开启"
        self.auto_task = None
        self.auto_last_voice_key = None
        self._last_voice_command = None
        self._last_voice_command_at = 0.0
        self.shutdown_when_idle = os.environ.get("BODY_COLLECTOR_SHUTDOWN_WHEN_IDLE") == "1"
        self._had_authenticated_client = False
        self._idle_shutdown_task = None

        self._setup_voice()

    def _preview_size_for_orientation(self, orientation: str):
        raw_w, raw_h = self.settings.camera.width, self.settings.camera.height
        if orientation in {"portrait_cw", "portrait_ccw"}:
            output_w, output_h = raw_h, raw_w
            target_h = self.settings.gui.preview_width
            return max(1, round(target_h * output_w / output_h)), target_h
        target_w = self.settings.gui.preview_width
        return target_w, max(1, round(target_w * raw_h / raw_w))

    def _setup_voice(self):
        if self.settings.voice.enabled:
            try:
                self.voice_recognizer = VoiceRecognizer(self.settings.voice.model_path)
                self.voice_synthesizer = VoiceSynthesizer(
                    voice=self.settings.voice.tts_voice,
                    rate=self.settings.voice.tts_rate,
                    volume=self.settings.voice.tts_volume
                )
                recognition_started = self.voice_recognizer.start_listening(
                    self._on_voice_command,
                    self._on_voice_activity
                )
                if not recognition_started:
                    self.voice_recognizer = None
                    logger.info("Voice commands are disabled until a complete Vosk model is installed")
                logger.info("Voice output initialized")
            except Exception as e:
                logger.error(f"Failed to setup voice: {e}")

    def _on_voice_activity(self, is_active: bool):
        """Broadcast voice activity status to all clients"""
        if self.loop and not self.loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._broadcast_voice_activity(is_active),
                    self.loop
                )
            except Exception:
                pass

    def _on_voice_command(self, text: str):
        # Ignore audio fed back from our own speakers.
        if self.voice_synthesizer and self.voice_synthesizer.is_speaking:
            return
        command = self.voice_parser.execute_command(text)
        if command == VoiceCommand.UNKNOWN:
            return
        now = time.monotonic()
        # Partial and final Vosk results often contain the same command.
        if command == self._last_voice_command and now - self._last_voice_command_at < 1.5:
            return
        self._last_voice_command = command
        self._last_voice_command_at = now
        if command == VoiceCommand.START_CAPTURE:
            if self.loop and not self.loop.is_closed():
                try:
                    asyncio.run_coroutine_threadsafe(self._handle_capture(), self.loop)
                except Exception:
                    pass
        elif command == VoiceCommand.STOP_CAPTURE:
            self.capture_cancelled = True
            self.is_capturing = False
        elif command == VoiceCommand.CANCEL:
            self.capture_cancelled = True
            self.is_capturing = False
        elif command == VoiceCommand.FINISH:
            if self.loop and not self.loop.is_closed():
                try:
                    asyncio.run_coroutine_threadsafe(self._handle_finish(), self.loop)
                except Exception:
                    pass

    def _build_capture_config(self, options: dict = None) -> CaptureConfig:
        options = options or {}
        return CaptureConfig(
            save_rgb=bool(options.get("save_rgb", self.settings.storage.save_rgb)),
            save_depth=bool(options.get("save_depth", self.settings.storage.save_depth)),
            save_pointcloud=bool(options.get("save_pointcloud", self.settings.storage.save_pointcloud)),
            colored_pointcloud=bool(options.get("colored_pointcloud", self.settings.storage.colored_pointcloud)),
            pointcloud_binary=bool(options.get("pointcloud_binary", self.settings.storage.pointcloud_binary)),
            quality_check=bool(options.get("quality_check", self.settings.storage.quality_check)),
            min_depth_coverage=self.settings.storage.min_depth_coverage,
            min_color_brightness=self.settings.storage.min_color_brightness,
            max_color_brightness=self.settings.storage.max_color_brightness
        )

    @staticmethod
    def _distance_payload(info):
        return {
            "distance_mm": info.distance_mm,
            "status": info.status.value,
            "message": info.message,
            "confidence": info.confidence,
            "full_body_visible": info.full_body_visible,
            "visibility_score": info.visibility_score,
            "capture_quality": info.to_capture_quality(),
        }

    async def _send_error(self, websocket, message: str):
        try:
            await websocket.send(json.dumps({"type": "error", "message": message}, ensure_ascii=False))
        except Exception:
            pass

    def _speak_auto(self, key: str, text: str):
        if self.auto_last_voice_key == key:
            return
        self.auto_last_voice_key = key
        if self.voice_synthesizer:
            self.voice_synthesizer.speak(text, blocking=False)

    async def _broadcast_auto_status(self):
        await self._broadcast({
            "type": "auto_capture_status",
            "data": {
                "enabled": self.auto_capture_enabled,
                "stable_frames": len(self.auto_stable_distances),
                "required_frames": self.auto_required_frames,
                "captured": self.auto_captured_count,
                "target_count": self.auto_target_count,
                "state": self.auto_state,
                "message": self.auto_message
            }
        })

    def _set_auto_waiting(self, state: str, message: str, reset_stability: bool = True):
        if reset_stability:
            self.auto_stable_distances = []
        self.auto_state = state
        self.auto_message = message

    async def _start_auto_capture(self, data: dict = None):
        data = data or {}
        if not self.depth_analyzer.pose_analyzer.available:
            self.auto_capture_enabled = False
            self.auto_state = "error"
            self.auto_message = "MediaPipe姿态模型不可用，自动采集已禁用；可使用手动强制采集"
            await self._broadcast_auto_status()
            return
        self.auto_capture_options = data.get("options") or {}
        self.auto_required_frames = min(
            _MAX_AUTO_STABLE_FRAMES,
            max(1, int(data.get("stable_frames", 10)))
        )
        self.auto_max_distance_delta_mm = min(
            _MAX_AUTO_DISTANCE_DELTA_MM,
            max(1.0, float(data.get("max_distance_delta_mm", 30.0)))
        )
        self.auto_target_count = min(
            _MAX_AUTO_CAPTURE_COUNT,
            max(1, int(data.get("capture_count", 3)))
        )
        self.auto_capture_interval_sec = min(
            _MAX_AUTO_CAPTURE_INTERVAL_SEC,
            max(0.1, float(data.get("capture_interval_sec", 1.0)))
        )
        self.auto_capture_enabled = True
        self.auto_stable_distances = []
        self.auto_captured_count = 0
        self.auto_state = "waiting"
        self.auto_message = "自动采集已开启，请站到相机前方"
        self.auto_last_voice_key = None
        self._speak_auto("started", "自动采集已开启，请站到相机前方。")
        await self._broadcast_auto_status()

    async def _stop_auto_capture(self, speak: bool = True):
        self.auto_capture_enabled = False
        self.auto_stable_distances = []
        self.auto_state = "stopped"
        self.auto_message = "自动采集已停止"
        if speak:
            self.auto_last_voice_key = None
            self._speak_auto("stopped", "自动采集已停止。")
        await self._broadcast_auto_status()

    async def _update_auto_capture(self, distance_info):
        if not self.auto_capture_enabled:
            return
        if self.auto_task and not self.auto_task.done():
            return

        if distance_info is None or not distance_info.ready:
            if self.auto_state != "waiting":
                self.auto_last_voice_key = None
            message = distance_info.recommended_action if distance_info else "等待人体进入画面"
            self._set_auto_waiting("waiting", message)
            await self._broadcast_auto_status()
            return

        distance_mm = float(distance_info.distance_mm)
        self.auto_stable_distances.append(distance_mm)
        if len(self.auto_stable_distances) > self.auto_required_frames:
            self.auto_stable_distances = self.auto_stable_distances[-self.auto_required_frames:]

        distance_delta = max(self.auto_stable_distances) - min(self.auto_stable_distances)
        if distance_delta > self.auto_max_distance_delta_mm:
            self.auto_stable_distances = [distance_mm]
            self.auto_state = "stabilizing"
            self.auto_message = "距离合适，请保持不动"
            self._speak_auto("hold_still", "距离合适，请保持不动。")
            await self._broadcast_auto_status()
            return

        self.auto_state = "stabilizing"
        self.auto_message = "距离合适，请保持不动"
        self._speak_auto("hold_still", "距离合适，请保持不动。")
        await self._broadcast_auto_status()

        if len(self.auto_stable_distances) >= self.auto_required_frames:
            self.auto_task = asyncio.create_task(self._run_auto_capture_batch())

    async def _run_auto_capture_batch(self):
        self.auto_state = "capturing"
        self.auto_message = "姿态稳定，开始自动采集"
        self.auto_stable_distances = []
        self._speak_auto("batch_started", "姿态稳定，开始自动采集。")
        await self._broadcast_auto_status()

        while self.auto_capture_enabled and self.auto_captured_count < self.auto_target_count:
            capture_options = {**self.auto_capture_options, "_suppress_voice": True, "_auto_capture": True}
            result = await self._handle_capture(capture_options)
            next_index = self.auto_captured_count + 1
            if result and result.success:
                self.auto_captured_count = next_index
                self.auto_message = f"已采集第 {self.auto_captured_count} 组"
                self._speak_auto(f"capture_success_{self.auto_captured_count}", f"已采集第 {self.auto_captured_count} 组。")
            else:
                self.auto_message = f"第 {next_index} 组采集失败，请保持姿态"
                self._speak_auto(f"capture_failed_{next_index}", f"第 {next_index} 组采集失败，请保持姿态。")

            await self._broadcast_auto_status()
            if self.auto_captured_count >= self.auto_target_count:
                break
            await asyncio.sleep(self.auto_capture_interval_sec)

        if self.auto_capture_enabled and self.auto_captured_count >= self.auto_target_count:
            self.auto_capture_enabled = False
            self.auto_state = "completed"
            self.auto_message = f"自动采集完成，共采集 {self.auto_captured_count} 组数据"
            self.auto_last_voice_key = None
            self._speak_auto("completed", f"自动采集完成，共采集 {self.auto_captured_count} 组数据。")
            await self._broadcast_auto_status()

    async def _handle_capture(self, options: dict = None):
        if self.capture_lock.locked():
            await self._broadcast({
                "type": "capture_result",
                "data": {"success": False, "error": "正在采集中，请稍候"}
            })
            return None

        async with self.capture_lock:
            self.is_capturing = True
            self.capture_cancelled = False
            try:
                suppress_voice = bool((options or {}).get("_suppress_voice"))
                force_capture = bool((options or {}).get("force"))
                auto_capture = bool((options or {}).get("_auto_capture"))
                config = self._build_capture_config(options)

                await asyncio.sleep(1)

                if self.capture_cancelled:
                    await self._broadcast({
                        "type": "capture_result",
                        "data": {"success": False, "error": "采集已取消"}
                    })
                    return None

                async with self.camera_lock:
                    frames = await asyncio.to_thread(self.camera.get_frames)
                if not frames:
                    await self._broadcast({
                        "type": "capture_result",
                        "data": {"success": False, "error": "未获取到相机画面"}
                    })
                    return None

                if frames.depth is not None:
                    body_info = await asyncio.to_thread(
                        self.depth_analyzer.analyze_distance,
                        frames.depth,
                        frames.color,
                        frames.depth_scale,
                        wait_for_pose=True,
                    )
                    if not body_info.ready and not force_capture:
                        warning = {
                            "quality": body_info.to_capture_quality(),
                            "message": body_info.message,
                            "options": {key: value for key, value in (options or {}).items() if not key.startswith("_")},
                        }
                        if auto_capture:
                            await self._broadcast({"type": "capture_result", "data": {"success": False, "error": body_info.message}})
                        else:
                            await self._broadcast({"type": "capture_quality_warning", "data": warning})
                        return None
                else:
                    await self._broadcast({
                        "type": "capture_result",
                        "data": {"success": False, "error": "没有可用的深度数据"}
                    })
                    return None

                if self.voice_synthesizer and not suppress_voice:
                    self.voice_synthesizer.speak("开始采集，请保持姿势不动。", blocking=False)

                point_cloud = None
                if config.save_pointcloud:
                    point_cloud = await asyncio.to_thread(
                        self.camera.generate_point_cloud,
                        frames,
                        colored=config.colored_pointcloud,
                        stride=self.settings.storage.pointcloud_stride
                    )

                intrinsics = await asyncio.to_thread(self.camera.get_camera_intrinsics)
                quality_snapshot = body_info.to_capture_quality()
                pose_metadata = self.depth_analyzer.build_pose_metadata(
                    body_info, frames.depth, frames.depth_scale, intrinsics
                ) if body_info.landmarks_2d and intrinsics else None
                body_mask, body_bbox, mask_source = self.depth_analyzer.get_body_segmentation()
                calibration_snapshot = await asyncio.to_thread(
                    self.camera.get_calibration_snapshot,
                    frames.depth_scale,
                    self.settings.camera.calibration_version,
                )
                raw_context = (options or {}).get("capture_context") or {}
                capture_context = {
                    **raw_context,
                    "forced_capture": force_capture,
                    "quality_ready": bool(body_info.ready),
                }
                result = await asyncio.to_thread(
                    self.data_collector.capture,
                    frames,
                    point_cloud,
                    config,
                    intrinsics,
                    quality_snapshot,
                    pose_metadata,
                    calibration_snapshot,
                    body_mask,
                    body_bbox,
                    mask_source,
                    capture_context,
                )

                await self._broadcast({
                    "type": "capture_result",
                    "data": {
                        "session_id": result.session_id,
                        "capture_id": result.capture_id,
                        "success": result.success,
                        "rgb_path": result.rgb_path,
                        "depth_path": result.depth_path,
                        "depth_preview_path": result.depth_preview_path,
                        "pointcloud_path": result.pointcloud_path,
                        "pose_path": result.pose_path,
                        "mask_path": result.mask_path,
                        "calibration_path": result.calibration_path,
                        "bbox": result.bbox,
                        "quality": result.quality,
                        "error": result.error
                    }
                })

                if self.voice_synthesizer and not suppress_voice:
                    if result.success:
                        self.voice_synthesizer.speak("采集完成。", blocking=False)
                    else:
                        self.voice_synthesizer.speak("采集失败，请重试。", blocking=False)
                return result
            finally:
                self.is_capturing = False

    async def _handle_finish(self):
        if self.voice_synthesizer:
            count = self.data_collector.get_capture_count()
            self.voice_synthesizer.speak(f"采集完成，共采集{count}组数据。", blocking=False)

        self.data_collector.close_session()
        await self._broadcast({
            "type": "session_finished",
            "data": {"capture_count": self.data_collector.get_capture_count()}
        })

    async def _broadcast_voice_activity(self, is_active: bool):
        """Broadcast voice activity status"""
        await self._broadcast({
            "type": "voice_activity",
            "data": {"active": is_active}
        })

    async def _send_camera_status(self, websocket=None, action: str = "status"):
        status = self.camera.get_status()
        status["orientation"] = self.camera.orientation
        status["action"] = action
        message = {
            "type": "camera_status",
            "data": status
        }
        if websocket is not None and websocket not in self.clients:
            try:
                await websocket.send(json.dumps(message, ensure_ascii=False))
            except Exception:
                pass
            return
        await self._broadcast(message)

    async def _handle_connect_camera(self, websocket=None, data: dict = None):
        if self.capture_lock.locked():
            await self._send_error(websocket, "正在采集中，请采集结束后再连接摄像头")
            return

        async with self.camera_lock:
            data = data or {}
            ok = await asyncio.to_thread(
                self.camera.connect,
                width=self.settings.camera.width,
                height=self.settings.camera.height,
                fps=self.settings.camera.fps,
                params_file=self.settings.camera.params_file,
                device_id=data.get("device_id", ""),
            )
            if ok:
                logger.info("Camera connected by client request")
            else:
                logger.warning(f"Camera connect failed: {self.camera.get_status().get('message')}")
            await self._send_camera_status(websocket, action="connect")

    async def _handle_disconnect_camera(self, websocket=None):
        if self.capture_lock.locked():
            await self._send_error(websocket, "正在采集中，请采集结束后再断开摄像头")
            return

        async with self.camera_lock:
            await asyncio.to_thread(self.camera.release)
            self.depth_analyzer.reset()
            self._last_color_preview = ""
            self._last_depth_preview = ""
            self._preview_miss_count = 0
            await self._broadcast({
                "type": "preview_frame",
                "data": {
                    "color": "",
                    "depth": "",
                    "distance": {
                        "distance_mm": 0,
                        "status": DistanceStatus.NO_DATA.value,
                        "message": "摄像头未连接",
                        "confidence": 0
                    }
                }
            })
            await self._send_camera_status(websocket, action="disconnect")

    async def _process_http_request(self, connection, request):
        if request.path == "/health":
            origin = request.headers.get("Origin")
            body = json.dumps({
                "ok": True,
                "service": "body-posture-backend",
                "host": self.host,
                "port": self.port,
                "clients": len(self.clients)
            }, ensure_ascii=False).encode("utf-8")
            headers = Headers([
                ("Content-Type", "application/json"),
                *_cors_headers_for_origin(origin)
            ])
            return Response(200, "OK", headers, body)

        if request.path == "/auth-token":
            origin = request.headers.get("Origin")
            if origin:
                if not _is_allowed_auth_origin(origin):
                    body = b"Forbidden origin"
                    headers = Headers([("Content-Type", "text/plain; charset=utf-8")])
                    return Response(403, "Forbidden", headers, body)
                cors_headers = _cors_headers_for_origin(origin)
            elif not _is_local_connection(connection):
                body = b"Forbidden"
                headers = Headers([("Content-Type", "text/plain; charset=utf-8")])
                return Response(403, "Forbidden", headers, body)
            else:
                cors_headers = []

            body = json.dumps({"token": self.auth_token}).encode("utf-8")
            headers = Headers([("Content-Type", "application/json"), *cors_headers])
            return Response(200, "OK", headers, body)
        return None

    async def _handle_client(self, websocket):
        try:
            remote = websocket.remote_address
        except Exception:
            remote = "unknown"

        try:
            auth_msg = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            data = json.loads(auth_msg)
            if data.get("type") != "auth" or data.get("token") != self.auth_token:
                await websocket.close(4001, "Authentication failed")
                logger.warning(f"Authentication failed from {remote}")
                return
        except (
            asyncio.TimeoutError,
            json.JSONDecodeError,
            websockets.exceptions.ConnectionClosedOK,
            websockets.exceptions.ConnectionClosedError,
        ):
            try:
                await websocket.close(4001, "Authentication timeout")
            except Exception:
                pass
            logger.warning(f"Authentication timeout from {remote}")
            return

        self.clients.add(websocket)
        self._had_authenticated_client = True
        if self._idle_shutdown_task and not self._idle_shutdown_task.done():
            self._idle_shutdown_task.cancel()
        self._idle_shutdown_task = None
        logger.info(f"Client connected: {remote}")
        try:
            await websocket.send(json.dumps({"type": "auth_success"}, ensure_ascii=False))
        except Exception:
            self.clients.discard(websocket)
            return

        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    await self._process_message(websocket, data)
                except json.JSONDecodeError:
                    await self._send_error(websocket, "请求格式无效")
                except Exception as e:
                    logger.error(f"Failed to process message: {e}")
                    safe_msg = _ERROR_MESSAGES.get(type(e), "服务器内部错误，请稍后重试")
                    try:
                        await self._send_error(websocket, safe_msg)
                    except Exception:
                        pass
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.debug(f"Client connection error: {e}")
        finally:
            self.clients.discard(websocket)
            logger.info(f"Client disconnected: {remote}")
            if len(self.clients) == 0:
                try:
                    await self._stop_preview()
                except Exception:
                    pass
                if self.shutdown_when_idle and self._had_authenticated_client and not self.is_shutting_down:
                    if not self._idle_shutdown_task or self._idle_shutdown_task.done():
                        self._idle_shutdown_task = asyncio.create_task(self._shutdown_after_idle())

    async def _shutdown_after_idle(self, delay: float = 5.0):
        """Allow a page refresh/reconnect, then stop a browser-launched session."""
        try:
            await asyncio.sleep(delay)
            if self.clients or self.is_shutting_down:
                return
            logger.info("No frontend client reconnected; stopping the application services")
            self.is_shutting_down = True
            if hasattr(self, "_ws_server") and self._ws_server:
                self._ws_server.close()
        except asyncio.CancelledError:
            pass

    async def _process_message(self, websocket, data: dict):
        msg_type = data.get("type")

        if msg_type == "start_preview":
            await self._start_preview(websocket)
        elif msg_type == "stop_preview":
            await self._stop_preview()
        elif msg_type == "capture_single":
            await self._handle_capture(data.get("options"))
        elif msg_type == "connect_camera":
            await self._handle_connect_camera(websocket, data)
        elif msg_type == "disconnect_camera":
            await self._handle_disconnect_camera(websocket)
        elif msg_type == "get_camera_status":
            await self._send_camera_status(websocket, action="status")
        elif msg_type == "set_camera_orientation":
            orientation = self.camera.set_orientation(data.get("orientation"))
            self.frame_processor.preview_size = self._preview_size_for_orientation(orientation)
            self.settings.camera.orientation = orientation
            config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config.json")
            save_settings(self.settings, config_path)
            self.depth_analyzer.reset()
            self._last_color_preview = ""
            self._last_depth_preview = ""
            self._preview_miss_count = 0
            await self._send_camera_status(websocket, action="orientation")
        elif msg_type == "start_auto_capture":
            await self._start_auto_capture(data)
        elif msg_type == "stop_auto_capture":
            await self._stop_auto_capture()
        elif msg_type == "create_session":
            raw_session_name = data.get("session_name") or ""
            session_name = _validate_field(raw_session_name, "session_name", _SAFE_PATTERN) if raw_session_name else ""
            session_id = self.data_collector.create_session(session_name or None, data.get("subject"))
            try:
                await websocket.send(json.dumps({
                    "type": "session_created",
                    "data": {"session_id": session_id}
                }, ensure_ascii=False))
            except Exception:
                pass
        elif msg_type == "get_distance":
            async with self.camera_lock:
                depth_frame = await asyncio.to_thread(self.camera.get_frames)
            if depth_frame and depth_frame.depth is not None:
                distance_info = await asyncio.to_thread(
                    self.depth_analyzer.analyze_distance,
                    depth_frame.depth,
                    depth_frame.color,
                    depth_frame.depth_scale,
                )
                try:
                    await websocket.send(json.dumps({
                        "type": "distance_update",
                        "data": self._distance_payload(distance_info)
                    }))
                except Exception:
                    pass
        elif msg_type == "speak":
            text = _validate_field(data.get("text", ""), "text")
            if self.voice_synthesizer:
                self.voice_synthesizer.speak(text, blocking=False)
        elif msg_type == "finish_session":
            await self._handle_finish()
        elif msg_type == "get_sessions":
            sessions = self.data_collector.get_session_list()
            try:
                await websocket.send(json.dumps({
                    "type": "session_list",
                    "data": {"sessions": sessions}
                }, ensure_ascii=False))
            except Exception:
                pass
        elif msg_type == "get_captures":
            captures = self.data_collector.get_captures()
            try:
                await websocket.send(json.dumps({
                    "type": "capture_list",
                    "data": {"captures": captures, "count": len(captures)}
                }, ensure_ascii=False))
            except Exception:
                pass
        elif msg_type == "get_capture_image":
            filename = _validate_field(data.get("filename", ""), "filename", _FILENAME_PATTERN)
            image_b64 = self.data_collector.get_capture_image(filename)
            try:
                await websocket.send(json.dumps({
                    "type": "capture_image",
                    "data": {"filename": filename, "image": image_b64}
                }, ensure_ascii=False))
            except Exception:
                pass
        elif msg_type == "review_capture":
            capture_id = _validate_field(data.get("capture_id", ""), "filename", _FILENAME_PATTERN)
            review = self.data_collector.update_capture_review(
                capture_id,
                data.get("status"),
                data.get("reviewer_id", ""),
                data.get("notes", ""),
            )
            try:
                await websocket.send(json.dumps({
                    "type": "review_updated",
                    "data": {"capture_id": capture_id, "qc_status": review.get("qc_status")},
                }, ensure_ascii=False))
            except Exception:
                pass
        elif msg_type == "select_session":
            session_name = data.get("session_name")
            if session_name and self.data_collector.select_session(session_name):
                try:
                    await websocket.send(json.dumps({
                        "type": "session_created",
                        "data": {"session_id": session_name}
                    }, ensure_ascii=False))
                except Exception:
                    pass
            else:
                await self._send_error(websocket, f"Session not found: {session_name}")
        elif msg_type == "exit_app":
            if not _is_local_connection(websocket):
                await self._send_error(websocket, "exit_app is only allowed from localhost")
                return
            logger.info("Exit command received from client")
            await self._broadcast({
                "type": "exit_confirm",
                "data": {"message": "系统即将关闭"}
            })
            await asyncio.sleep(0.5)
            self._shutdown()
        else:
            await self._send_error(websocket, f"Unknown message type: {msg_type}")

    async def _start_preview(self, websocket):
        self.is_previewing = True
        if self.preview_task and not self.preview_task.done():
            self.preview_task.cancel()
            try:
                await self.preview_task
            except (asyncio.CancelledError, Exception):
                pass
        self.preview_task = asyncio.create_task(self._preview_loop())

    async def _stop_preview(self):
        self.is_previewing = False
        if self.preview_task and not self.preview_task.done():
            self.preview_task.cancel()
            try:
                await self.preview_task
            except (asyncio.CancelledError, Exception):
                pass
        self.preview_task = None

    async def _preview_loop(self):
        try:
            import time
            while self.is_previewing and self.clients:
                start_time = time.time()
                if self.is_capturing:
                    await asyncio.sleep(max(0.1, 1.0 / max(1, self.settings.gui.preview_fps)))
                    continue
                
                async with self.camera_lock:
                    frames = await asyncio.to_thread(self.camera.get_frames)
                color_preview = ""
                depth_preview = ""
                distance_data = None

                if frames and frames.color is not None:
                    color_preview = await asyncio.to_thread(
                        self.frame_processor.encode_preview_fast, frames.color, True
                    )
                    if color_preview:
                        self._last_color_preview = color_preview

                if frames and frames.depth is not None:
                    depth_preview, distance_info = await asyncio.gather(
                        asyncio.to_thread(
                            self.frame_processor.encode_depth_preview_fast,
                            frames.depth,
                            frames.depth_scale,
                        ),
                        asyncio.to_thread(
                            self.depth_analyzer.analyze_distance,
                            frames.depth,
                            frames.color,
                            depth_scale=frames.depth_scale,
                        ),
                    )
                    distance_data = self._distance_payload(distance_info)
                    if depth_preview:
                        self._last_depth_preview = depth_preview
                        self._preview_miss_count = 0
                    else:
                        self._preview_miss_count += 1
                    await self._update_auto_capture(distance_info)
                else:
                    self._preview_miss_count += 1
                    await self._update_auto_capture(None)
                    distance_data = {
                        "distance_mm": 0,
                        "status": DistanceStatus.NO_DATA.value,
                        "message": self.camera.last_error or "摄像头未连接",
                        "confidence": 0
                    }

                # Brief camera timeouts should not replace the images with an
                # empty frame; doing so makes both previews visibly blink.
                if self._preview_miss_count <= 5:
                    color_preview = color_preview or self._last_color_preview
                    depth_preview = depth_preview or self._last_depth_preview

                await self._broadcast({
                    "type": "preview_frame",
                    "data": {
                        "color": color_preview,
                        "depth": depth_preview,
                        "distance": distance_data
                    }
                })

                elapsed = time.time() - start_time
                target_interval = 1.0 / self.settings.gui.preview_fps
                sleep_time = max(0, target_interval - elapsed)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Preview loop error: {e}")

    async def _broadcast(self, message: dict):
        if not self.clients:
            return
        try:
            message_str = json.dumps(message, ensure_ascii=False)
            clients = list(self.clients)

            async def send_one(client):
                try:
                    await asyncio.wait_for(client.send(message_str), timeout=0.5)
                    return None
                except Exception:
                    return client

            closed = await asyncio.gather(*(send_one(client) for client in clients))
            for client in filter(None, closed):
                self.clients.discard(client)
                try:
                    await client.close(code=1011, reason="client too slow")
                except Exception:
                    pass
        except Exception:
            pass

    async def start(self):
        try:
            self.loop = asyncio.get_event_loop()

            import os
            token_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".ws_token")
            with open(token_file, 'w') as f:
                f.write(self.auth_token)

            camera_ok = self.camera.connect(
                width=self.settings.camera.width,
                height=self.settings.camera.height,
                fps=self.settings.camera.fps,
                params_file=self.settings.camera.params_file
            )

            self._ws_server = await websockets.serve(
                self._handle_client, self.host, self.port,
                process_request=self._process_http_request,
                ping_interval=10,
                ping_timeout=10,
                close_timeout=3,
                max_queue=16,
                max_size=2 * 1024 * 1024,
            )
            logger.info(f"WebSocket server started on ws://{self.host}:{self.port}")

            if self.shutdown_when_idle:
                # Also avoid leaving services behind if the browser never
                # authenticates because it was closed during startup.
                self._idle_shutdown_task = asyncio.create_task(
                    self._shutdown_after_idle(delay=30.0)
                )

            if camera_ok:
                logger.info("Camera ready")
            else:
                logger.warning(f"Camera not connected: {self.camera.get_status().get('message')}")

            if self.voice_synthesizer:
                self.voice_synthesizer.speak("系统已启动", blocking=False)

            await self._ws_server.wait_closed()
            self.stop()
        except Exception as e:
            logger.error(f"Server error: {e}")
            raise

    def stop(self):
        self.is_previewing = False
        self.is_capturing = False
        self.auto_capture_enabled = False
        if self.voice_recognizer:
            try:
                self.voice_recognizer.release()
            except Exception:
                pass
            self.voice_recognizer = None
        try:
            self.depth_analyzer.close()
        except Exception:
            pass
        if hasattr(self, '_ws_server') and self._ws_server:
            try:
                self._ws_server.close()
            except Exception:
                pass
        if self.camera:
            try:
                self.camera.release()
            except Exception:
                pass
        self.loop = None
        logger.info("Server stopped")

    def get_auth_info(self) -> dict:
        return {"token": self.auth_token, "host": self.host, "port": self.port}

    def _shutdown(self):
        logger.info("Shutting down application...")
        self.is_shutting_down = True
        self.is_previewing = False
        self.is_capturing = False
        try:
            self.stop()
        except Exception:
            pass

        import os
        os._exit(0)


async def main():
    server = WebSocketServer()
    try:
        await server.start()
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    asyncio.run(main())
