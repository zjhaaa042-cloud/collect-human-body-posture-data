import asyncio
import json
import websockets
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


class WebSocketServer:
    def __init__(self, host: str = "localhost", port: int = 8765):
        self.host = host
        self.port = port
        self.clients: Set[websockets.WebSocketServerProtocol] = set()

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
        if self.loop:
            asyncio.run_coroutine_threadsafe(
                self._broadcast_voice_activity(is_active),
                self.loop
            )

    def _on_voice_command(self, text: str):
        command = self.voice_parser.execute_command(text)
        if command == VoiceCommand.START_CAPTURE:
            if self.loop:
                asyncio.run_coroutine_threadsafe(self._handle_capture(), self.loop)
        elif command == VoiceCommand.STOP_CAPTURE:
            self.is_capturing = False
        elif command == VoiceCommand.FINISH:
            if self.loop:
                asyncio.run_coroutine_threadsafe(self._handle_finish(), self.loop)

    async def _handle_capture(self):
        self.is_capturing = True
        if self.voice_synthesizer:
            self.voice_synthesizer.speak("开始采集，请保持姿势不动。", blocking=False)

        await asyncio.sleep(1)

        frames = self.camera.get_frames()
        if frames:
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
                    self.is_capturing = False
                    return

            point_cloud = self.camera.generate_point_cloud(frames)
            config = CaptureConfig(
                save_rgb=self.settings.storage.save_rgb,
                save_depth=self.settings.storage.save_depth,
                save_pointcloud=self.settings.storage.save_pointcloud,
                colored_pointcloud=self.settings.storage.colored_pointcloud,
                quality_check=self.settings.storage.quality_check,
                min_depth_coverage=self.settings.storage.min_depth_coverage,
                min_color_brightness=self.settings.storage.min_color_brightness,
                max_color_brightness=self.settings.storage.max_color_brightness
            )
            intrinsics = self.camera.get_camera_intrinsics()
            result = self.data_collector.capture(frames, point_cloud, config, camera_intrinsics=intrinsics)

            await self._broadcast({
                "type": "capture_result",
                "data": {
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
                    self.voice_synthesizer.speak(f"采集完成。", blocking=False)
                else:
                    self.voice_synthesizer.speak("采集失败，请重试。", blocking=False)

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

    async def _handle_client(self, websocket):
        self.clients.add(websocket)
        logger.info(f"Client connected: {websocket.remote_address}")

        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    await self._process_message(websocket, data)
                except json.JSONDecodeError:
                    await websocket.send(json.dumps({"type": "error", "message": "Invalid JSON"}))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.clients.discard(websocket)
            logger.info(f"Client disconnected: {websocket.remote_address}")
            if len(self.clients) == 0:
                await self._stop_preview()

    async def _process_message(self, websocket, data: dict):
        msg_type = data.get("type")

        if msg_type == "start_preview":
            await self._start_preview(websocket)
        elif msg_type == "stop_preview":
            await self._stop_preview()
        elif msg_type == "capture_single":
            await self._handle_capture()
        elif msg_type == "create_session":
            session_name = data.get("session_name")
            session_id = self.data_collector.create_session(session_name)
            await websocket.send(json.dumps({
                "type": "session_created",
                "data": {"session_id": session_id}
            }))
        elif msg_type == "get_distance":
            distance = self.camera.get_center_distance()
            depth_frame = self.camera.get_frames()
            if depth_frame and depth_frame.depth is not None:
                distance_info = self.depth_analyzer.analyze_distance(depth_frame.depth)
                await websocket.send(json.dumps({
                    "type": "distance_update",
                    "data": {
                        "distance_mm": distance_info.distance_mm,
                        "status": distance_info.status.value,
                        "message": distance_info.message
                    }
                }))
        elif msg_type == "speak":
            text = data.get("text", "")
            if self.voice_synthesizer:
                self.voice_synthesizer.speak(text, blocking=False)
        elif msg_type == "get_sessions":
            sessions = self.data_collector.get_session_list()
            await websocket.send(json.dumps({
                "type": "session_list",
                "data": {"sessions": sessions}
            }))
        elif msg_type == "get_captures":
            captures = self.data_collector.get_captures()
            await websocket.send(json.dumps({
                "type": "capture_list",
                "data": {"captures": captures, "count": len(captures)}
            }))
        elif msg_type == "get_capture_image":
            filename = data.get("filename", "")
            import re
            if not re.match(r'^[\w\-]+$', filename):
                await websocket.send(json.dumps({
                    "type": "capture_image",
                    "data": {"filename": filename, "image": ""}
                }))
            else:
                image_b64 = self.data_collector.get_capture_image(filename)
                await websocket.send(json.dumps({
                    "type": "capture_image",
                    "data": {"filename": filename, "image": image_b64}
                }))
        elif msg_type == "select_session":
            session_name = data.get("session_name")
            if session_name and self.data_collector.select_session(session_name):
                await websocket.send(json.dumps({
                    "type": "session_created",
                    "data": {"session_id": session_name}
                }))
            else:
                await websocket.send(json.dumps({
                    "type": "error",
                    "message": f"Session not found: {session_name}"
                }))
        elif msg_type == "exit_app":
            logger.info("Exit command received from client")
            await self._broadcast({
                "type": "exit_confirm",
                "data": {"message": "系统即将关闭"}
            })
            await asyncio.sleep(0.5)
            self._shutdown()

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
        message_str = json.dumps(message)
        closed = set()
        for client in list(self.clients):
            try:
                await client.send(message_str)
            except websockets.exceptions.ConnectionClosed:
                closed.add(client)
            except Exception:
                closed.add(client)
        for client in closed:
            self.clients.discard(client)

    async def start(self):
        try:
            self.loop = asyncio.get_event_loop()
            
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

            self._ws_server = await websockets.serve(self._handle_client, self.host, self.port)
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
        if hasattr(self, '_ws_server') and self._ws_server:
            self._ws_server.close()
        if self.camera:
            self.camera.release()
        if self.voice_recognizer:
            self.voice_recognizer.release()
        logger.info("Server stopped")

    def _shutdown(self):
        logger.info("Shutting down application...")
        self.stop()
        import subprocess, os
        subprocess.Popen('taskkill /F /IM python.exe /T', shell=True, creationflags=0x08)
        subprocess.Popen('taskkill /F /IM node.exe /T', shell=True, creationflags=0x08)
        os._exit(0)


async def main():
    server = WebSocketServer()
    try:
        await server.start()
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    asyncio.run(main())
