import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from loguru import logger

from backend.application.dual_workflow import DualCapturePersistenceError
from backend.server.ws_server import WebSocketServer
from run_backend import configure_file_logging


class FailureDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_latest_reader_sends_restored_state_or_empty_result(self):
        for found in (True, False):
            with self.subTest(found=found):
                server = WebSocketServer.__new__(WebSocketServer)
                state = {"subject_id": "S0026", "progress": {"captured": 3}, "next_yaw_deg": 135}
                result = {"found": found, "output_path": "D:/test"}
                if found:
                    result["state"] = state
                server.dual_workflow = SimpleNamespace(open_latest_session=mock.Mock(return_value=result))
                server._apply_dual_distance_target = mock.Mock()
                server._disarm_dual_voice_capture = mock.AsyncMock()
                server._emit_protocol_message = mock.AsyncMock()
                websocket = object()
                await server._open_latest_dual_session(websocket, {"output_path": "D:/test"})
                if found:
                    server._emit_protocol_message.assert_awaited_once_with(websocket, {
                        "type": "dual_session_state", "data": {**state, "event": "latest_opened"},
                    })
                    server._disarm_dual_voice_capture.assert_awaited_once()
                else:
                    server._emit_protocol_message.assert_awaited_once_with(websocket, {
                        "type": "latest_dual_session_result", "data": {"success": True, **result},
                    })
                    server._disarm_dual_voice_capture.assert_not_awaited()

    async def test_ui_and_voice_receive_write_failure_state_before_error(self):
        for trigger in ("ui", "voice"):
            with self.subTest(trigger=trigger):
                server = WebSocketServer.__new__(WebSocketServer)
                state = {"subject_id": "S0001", "reconciliation_required": True}
                error = DualCapturePersistenceError("injected write failure", state)
                server.dual_workflow = SimpleNamespace(capture_group=mock.AsyncMock(side_effect=error))
                server.capture_lock = asyncio.Lock()
                server.camera_lock = asyncio.Lock()
                server._disarm_dual_voice_capture = mock.AsyncMock()
                server._emit_protocol_message = mock.AsyncMock()
                server._queue_voice = mock.Mock()
                websocket = object() if trigger == "ui" else None
                with self.assertRaises(DualCapturePersistenceError):
                    await server._capture_dual_group(websocket, {
                        "subject_id": "S0001", "yaw_deg": 0, "ready": True, "trigger": trigger,
                    })
                server._emit_protocol_message.assert_awaited_once_with(websocket, {
                    "type": "dual_session_state", "data": {**state, "event": "write_failed"},
                })
                server._queue_voice.assert_not_called()


class FileLoggingTests(unittest.TestCase):
    def test_logging_records_chinese_exception_and_can_fail_without_stopping_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logs" / "app.log"
            settings = SimpleNamespace(log_file=str(path), log_level="INFO")
            sink = configure_file_logging(settings)
            self.assertIsNotNone(sink)
            try:
                try:
                    raise PermissionError("模拟文件占用")
                except PermissionError:
                    logger.exception("角度写入失败")
            finally:
                logger.remove(sink)
            text = path.read_text(encoding="utf-8")
            self.assertIn("PermissionError", text)
            self.assertIn("模拟文件占用", text)
            self.assertIn("角度写入失败", text)
            with mock.patch.object(logger, "add", side_effect=PermissionError("log locked")):
                self.assertIsNone(configure_file_logging(settings))
