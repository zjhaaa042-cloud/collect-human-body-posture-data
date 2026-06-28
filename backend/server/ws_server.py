import asyncio
import json
import re
import secrets
import websockets
from websockets.http11 import Response
from websockets.datastructures import Headers
from typing import Set, Callable, Any
from loguru import logger

from ..core.camera_manager import CameraManager
from ..core.depth_analyzer import DepthAnalyzer, DistanceStatus
from ..core.data_collector import DataCollector, CaptureConfig
from ..voice.recognizer import VoiceRecognizer
from ..voice.synthesizer import VoiceSynthesizer
from ..voice.command_parser import VoiceCommandParser, VoiceCommand
from ..utils.frame_processor import FrameProcessor
from ..config.settings import get_settings

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
        self.camera = CameraManager()
        self.depth_analyzer = DepthAnalyzer(
            target_distance_mm=self.settings.distance.target_distance_mm,
            tolerance_mm=self.settings.distance.tolerance_mm,
            roi_ratio=self.settings.distance.roi_ratio
        )
        self.data_collector = DataCollector(self.settings.storage.output_dir)
        self.frame_processor = FrameProcessor(
            preview_size=(self.settings.gui.preview_width, self.settings.gui.preview_height),
            jpeg_quality=self.settings.gui.jpeg_quality
        )

        self.voice_recognizer = None
        self.voice_synthesizer = None
        self.voice_parser = VoiceCommandParser()
        self.loop = None  # Store reference to main event loop

        self.is_previewing = False
        self.is_capturing = False
        self.is_shutting_down = False
        self.capture_lock = asyncio.Lock()
        self.preview_task = None

        self._setup_voice()

    def _setup_voice(self):
        if self.settings.voice.enabled:
            try:
                self.voice_recognizer = VoiceRecognizer(self.settings.voice.model_path)
                self.voice_synthesizer = VoiceSynthesizer(
                    voice=self.settings.voice.tts_voice,
                    rate=self.settings.voice.tts_rate,
                    volume=self.settings.voice.tts_volume
                )
                self.voice_recognizer.start_listening(
                    self._on_voice_command,
                    self._on_voice_activity
                )
                logger.info("Voice system initialized")
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
        command = self.voice_parser.execute_command(text)
        if command == VoiceCommand.START_CAPTURE:
            if self.loop and not self.loop.is_closed():
                try:
                    asyncio.run_coroutine_threadsafe(self._handle_capture(), self.loop)
                except Exception:
                    pass
        elif command == VoiceCommand.STOP_CAPTURE:
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

    async def _send_error(self, websocket, message: str):
        try:
            await websocket.send(json.dumps({"type": "error", "message": message}, ensure_ascii=False))
        except Exception:
            pass

    async def _handle_capture(self, options: dict = None):
        if self.capture_lock.locked():
            await self._broadcast({
                "type": "capture_result",
                "data": {"success": False, "error": "正在采集中，请稍候"}
            })
            return

        async with self.capture_lock:
            self.is_capturing = True
            try:
                config = self._build_capture_config(options)
                if self.voice_synthesizer:
                    self.voice_synthesizer.speak("开始采集，请保持姿势不动。", blocking=False)

                await asyncio.sleep(1)

                frames = self.camera.get_frames()
                if not frames:
                    await self._broadcast({
                        "type": "capture_result",
                        "data": {"success": False, "error": "未获取到相机画面"}
                    })
                    return

                if frames.depth is not None:
                    human_detected, _ = self.depth_analyzer.detect_human(frames.depth, frames.depth_scale)
                    if not human_detected:
                        if self.voice_synthesizer:
                            self.voice_synthesizer.speak("未识别到人体，请站在相机前方。", blocking=False)
                        await self._broadcast({
                            "type": "capture_result",
                            "data": {
                                "success": False,
                                "error": "未识别到人体，请站在相机前方"
                            }
                        })
                        return

                point_cloud = None
                if config.save_pointcloud:
                    point_cloud = self.camera.generate_point_cloud(
                        frames,
                        colored=config.colored_pointcloud,
                        stride=self.settings.storage.pointcloud_stride
                    )

                intrinsics = self.camera.get_camera_intrinsics()
                result = self.data_collector.capture(frames, point_cloud, config, camera_intrinsics=intrinsics)

                await self._broadcast({
                    "type": "capture_result",
                    "data": {
                        "session_id": result.session_id,
                        "capture_id": result.capture_id,
                        "success": result.success,
                        "rgb_path": result.rgb_path,
                        "depth_path": result.depth_path,
                        "pointcloud_path": result.pointcloud_path,
                        "error": result.error
                    }
                })

                if self.voice_synthesizer:
                    if result.success:
                        self.voice_synthesizer.speak("采集完成。", blocking=False)
                    else:
                        self.voice_synthesizer.speak("采集失败，请重试。", blocking=False)
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

    async def _process_http_request(self, connection, request):
        if request.path == "/auth-token":
            body = json.dumps({"token": self.auth_token}).encode("utf-8")
            headers = Headers([("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*")])
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
        logger.info(f"Client connected: {remote}")

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

    async def _process_message(self, websocket, data: dict):
        msg_type = data.get("type")

        if msg_type == "start_preview":
            await self._start_preview(websocket)
        elif msg_type == "stop_preview":
            await self._stop_preview()
        elif msg_type == "capture_single":
            await self._handle_capture(data.get("options"))
        elif msg_type == "create_session":
            session_name = _validate_field(data.get("session_name", ""), "session_name", _SAFE_PATTERN)
            session_id = self.data_collector.create_session(session_name)
            try:
                await websocket.send(json.dumps({
                    "type": "session_created",
                    "data": {"session_id": session_id}
                }, ensure_ascii=False))
            except Exception:
                pass
        elif msg_type == "get_distance":
            distance = self.camera.get_center_distance()
            depth_frame = self.camera.get_frames()
            if depth_frame and depth_frame.depth is not None:
                distance_info = self.depth_analyzer.analyze_distance(depth_frame.depth)
                try:
                    await websocket.send(json.dumps({
                        "type": "distance_update",
                        "data": {
                            "distance_mm": distance_info.distance_mm,
                            "status": distance_info.status.value,
                            "message": distance_info.message
                        }
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
                
                frames = self.camera.get_frames()
                if frames:
                    color_preview = ""
                    depth_preview = ""
                    distance_data = None

                    if frames.color is not None:
                        color_preview = self.frame_processor.encode_preview_fast(frames.color, is_rgb=True)

                    if frames.depth is not None:
                        depth_preview = self.frame_processor.encode_depth_preview_fast(frames.depth, frames.depth_scale)
                        distance_info = self.depth_analyzer.analyze_distance(frames.depth, depth_scale=frames.depth_scale)
                        distance_data = {
                            "distance_mm": distance_info.distance_mm,
                            "status": distance_info.status.value,
                            "message": distance_info.message,
                            "confidence": distance_info.confidence
                        }

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
            closed = set()
            for client in list(self.clients):
                try:
                    await client.send(message_str)
                except Exception:
                    closed.add(client)
            for client in closed:
                self.clients.discard(client)
        except Exception:
            pass

    async def start(self):
        try:
            self.loop = asyncio.get_event_loop()

            import os
            token_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".ws_token")
            with open(token_file, 'w') as f:
                f.write(self.auth_token)

            camera_ok = self.camera.initialize(
                width=self.settings.camera.width,
                height=self.settings.camera.height,
                fps=self.settings.camera.fps,
                params_file=self.settings.camera.params_file
            )

            if camera_ok:
                if not self.camera.start_stream():
                    logger.warning("Failed to start camera stream, using mock mode")
                    camera_ok = False
            else:
                logger.warning("Failed to initialize camera, using mock mode")

            self._ws_server = await websockets.serve(
                self._handle_client, self.host, self.port,
                process_request=self._process_http_request
            )
            logger.info(f"WebSocket server started on ws://{self.host}:{self.port}")

            if camera_ok:
                logger.info("Camera ready")
            else:
                logger.warning("Running in MOCK mode (no real camera)")

            if self.voice_synthesizer:
                self.voice_synthesizer.speak("系统已启动", blocking=False)

            await self._ws_server.wait_closed()
        except Exception as e:
            logger.error(f"Server error: {e}")
            raise

    def stop(self):
        self.is_previewing = False
        self.is_capturing = False
        if self.voice_recognizer:
            try:
                self.voice_recognizer.release()
            except Exception:
                pass
            self.voice_recognizer = None
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

    @staticmethod
    def _kill_process_on_port(port: int):
        import subprocess
        try:
            result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    pid = parts[-1]
                    if pid.isdigit():
                        subprocess.run(["taskkill", "/F", "/PID", pid],
                                       capture_output=True, timeout=5)
                        logger.info(f"Killed process {pid} on port {port}")
        except Exception as e:
            logger.debug(f"Failed to kill process on port {port}: {e}")

    def _shutdown(self):
        logger.info("Shutting down application...")
        self.is_shutting_down = True
        self.is_previewing = False
        self.is_capturing = False
        try:
            self.stop()
        except Exception:
            pass

        self._kill_process_on_port(3000)

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
