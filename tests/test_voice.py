import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from backend.config.settings import VoiceSettings
from backend.server.ws_server import WebSocketServer
from backend.voice.command_parser import VoiceCommand, VoiceCommandParser
from backend.voice.synthesizer import VoiceSynthesizer


class VoiceCommandParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = VoiceCommandParser()

    def test_only_complete_phrases_are_accepted(self):
        self.assertEqual(self.parser.parse("开始采集"), VoiceCommand.START_CAPTURE)
        self.assertEqual(self.parser.parse("重复提示。"), VoiceCommand.REPEAT)
        self.assertEqual(self.parser.parse("取 消 采 集"), VoiceCommand.CANCEL)
        for unsafe in ("开始", "采集", "拍照", "停止", "下一个", "好了", "完成"):
            self.assertEqual(self.parser.parse(unsafe), VoiceCommand.UNKNOWN)

    def test_phrase_must_match_after_normalization(self):
        self.assertEqual(
            self.parser.parse("请帮我开始采集"),
            VoiceCommand.UNKNOWN,
        )
        self.assertEqual(
            self.parser.recognition_phrases(),
            ("开始 采集", "重复 提示", "取消 采集"),
        )

    def test_legacy_enabled_does_not_disable_new_default_preferences(self):
        settings = VoiceSettings(enabled=False)
        self.assertTrue(settings.output_enabled)
        self.assertTrue(settings.recognition_enabled)


class VoiceSynthesizerQueueTests(unittest.TestCase):
    def test_nonblocking_requests_are_queued_instead_of_dropped(self):
        synthesizer = VoiceSynthesizer(online_timeout_seconds=0.5)
        started = threading.Event()
        release_first = threading.Event()
        played = []

        def fake_play(text, _generation):
            played.append(text)
            if text == "第一条":
                started.set()
                release_first.wait(2)
            return True

        synthesizer._play_sync = fake_play
        try:
            self.assertTrue(synthesizer.speak("第一条", blocking=False))
            self.assertTrue(started.wait(1))
            self.assertTrue(synthesizer.speak("第二条", blocking=False))
            release_first.set()
            synthesizer._queue.join()
            self.assertEqual(played, ["第一条", "第二条"])
        finally:
            release_first.set()
            synthesizer.release()

    def test_muting_stops_current_speech_and_clears_pending_requests(self):
        synthesizer = VoiceSynthesizer(online_timeout_seconds=0.5)
        started = threading.Event()
        release_first = threading.Event()
        played = []

        def fake_play(text, _generation):
            played.append(text)
            if text == "第一条":
                started.set()
                release_first.wait(2)
            return True

        synthesizer._play_sync = fake_play
        try:
            self.assertTrue(synthesizer.speak("第一条", blocking=False))
            self.assertTrue(started.wait(1))
            self.assertTrue(synthesizer.speak("不应播放", blocking=False))
            synthesizer.set_enabled(False)
            release_first.set()
            synthesizer._queue.join()
            self.assertEqual(played, ["第一条"])
        finally:
            release_first.set()
            synthesizer.release()


class DualVoiceWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def _server(self):
        owner = object()
        server = WebSocketServer.__new__(WebSocketServer)
        server.settings = SimpleNamespace(
            voice=SimpleNamespace(capture_arm_timeout_seconds=30)
        )
        server.voice_parser = VoiceCommandParser()
        server.voice_output_enabled = True
        server.voice_recognition_enabled = True
        server.voice_recognizer = SimpleNamespace(
            available=True,
            is_listening=True,
            initialization_error="",
        )
        server.voice_synthesizer = None
        server._voice_last_error = ""
        server._voice_input_active = False
        server._voice_tts_active = False
        server._voice_command_suppressed_until = 0.0
        server._last_voice_command = VoiceCommand.UNKNOWN
        server._last_voice_command_at = 0.0
        server._dual_voice_arm = None
        server._dual_voice_arm_task = None
        server.capture_lock = asyncio.Lock()
        server.camera_lock = asyncio.Lock()
        server.is_capturing = False
        server.clients = {owner}
        server.dual_workflow = SimpleNamespace(public_state=lambda: {
            "active": True,
            "subject_id": "S0001",
            "status": "ACTIVE",
            "reconciliation_required": False,
            "next_yaw_deg": 0,
            "anthropometry": {"complete": False},
        })
        server._camera_status_snapshot = mock.Mock(return_value={"dual_ready": True})
        server._emit_protocol_message = mock.AsyncMock()
        server._broadcast = mock.AsyncMock()
        server._queue_voice = mock.Mock(return_value=True)
        return server, owner

    async def test_partial_and_final_duplicate_command_is_executed_once(self):
        server, _owner = self._server()
        server.loop = SimpleNamespace(is_closed=lambda: False)

        def close_scheduled(coroutine, _loop):
            coroutine.close()

        with mock.patch(
            "backend.server.ws_server.asyncio.run_coroutine_threadsafe",
            side_effect=close_scheduled,
        ) as scheduled:
            server._on_voice_command("开始采集")
            server._on_voice_command("开始采集。")

        self.assertEqual(scheduled.call_count, 1)

    async def test_tts_activity_suspends_commands_and_resets_context(self):
        server, _owner = self._server()
        server.loop = None
        server.voice_recognizer.suspend_commands = mock.Mock()
        server.voice_recognizer.reset_context = mock.Mock()

        server._on_tts_activity(True)
        self.assertTrue(server._voice_tts_active)
        server.voice_recognizer.suspend_commands.assert_called_with(60.0)

        server._on_tts_activity(False)
        self.assertFalse(server._voice_tts_active)
        server.voice_recognizer.reset_context.assert_called_once()
        self.assertEqual(
            server.voice_recognizer.suspend_commands.call_args.args[0],
            0.75,
        )
        self.assertGreater(server._voice_command_suppressed_until, 0)

    async def test_arm_is_bound_to_subject_angle_distance_and_owner(self):
        server, owner = self._server()
        try:
            status = await server._arm_dual_voice_capture(owner, {
                "subject_id": "S0001",
                "yaw_deg": 0,
                "distance_mm": 2500,
            })
            self.assertTrue(status["capture_armed"])
            self.assertEqual(status["armed_subject_id"], "S0001")
            self.assertEqual(status["armed_yaw_deg"], 0)
            self.assertEqual(server._dual_voice_arm["distance_mm"], 2500)
            self.assertIs(server._dual_voice_arm["owner"], owner)
        finally:
            await server._disarm_dual_voice_capture(reason="test")

    async def test_start_capture_cannot_bypass_ready_arm(self):
        server, _owner = self._server()
        server._capture_dual_group = mock.AsyncMock()
        server._emit_voice_command_event = mock.AsyncMock()

        await server._execute_dual_voice_command(
            VoiceCommand.START_CAPTURE,
            "开始采集",
        )

        server._capture_dual_group.assert_not_awaited()
        server._emit_voice_command_event.assert_awaited_once()
        self.assertEqual(
            server._emit_voice_command_event.await_args.args[1],
            "rejected",
        )

    async def test_start_capture_consumes_arm_once(self):
        server, owner = self._server()
        server._capture_dual_group = mock.AsyncMock()
        server._emit_voice_command_event = mock.AsyncMock()
        await server._arm_dual_voice_capture(owner, {
            "subject_id": "S0001",
            "yaw_deg": 0,
            "distance_mm": 2500,
        })

        await server._execute_dual_voice_command(
            VoiceCommand.START_CAPTURE,
            "开始采集",
        )

        self.assertIsNone(server._dual_voice_arm)
        server._capture_dual_group.assert_awaited_once()
        payload = server._capture_dual_group.await_args.args[1]
        self.assertEqual(payload["trigger"], "voice")
        self.assertTrue(payload["ready"])

    async def test_task_change_invalidates_arm_before_capture(self):
        server, owner = self._server()
        server._capture_dual_group = mock.AsyncMock()
        server._emit_voice_command_event = mock.AsyncMock()
        await server._arm_dual_voice_capture(owner, {
            "subject_id": "S0001",
            "yaw_deg": 0,
            "distance_mm": 2500,
        })
        server.dual_workflow.public_state = lambda: {
            "active": True,
            "subject_id": "S0001",
            "status": "ACTIVE",
            "reconciliation_required": False,
            "next_yaw_deg": 45,
            "anthropometry": {"complete": False},
        }

        await server._execute_dual_voice_command(
            VoiceCommand.START_CAPTURE,
            "开始采集",
        )

        self.assertIsNone(server._dual_voice_arm)
        server._capture_dual_group.assert_not_awaited()
        self.assertEqual(
            server._emit_voice_command_event.await_args.args[1],
            "rejected",
        )

    async def test_arm_expires_and_wrong_owner_cannot_disarm_it(self):
        server, owner = self._server()
        other_owner = object()
        try:
            await server._arm_dual_voice_capture(owner, {
                "subject_id": "S0001",
                "yaw_deg": 0,
                "distance_mm": 2500,
            })
            self.assertFalse(await server._disarm_dual_voice_capture(
                reason="wrong_owner",
                owner=other_owner,
            ))
            token = server._dual_voice_arm["token"]
            server._dual_voice_arm_task.cancel()
            server._dual_voice_arm_task = None
            with mock.patch(
                "backend.server.ws_server.asyncio.sleep",
                new=mock.AsyncMock(),
            ):
                await server._expire_dual_voice_arm(token, 30)
            self.assertIsNone(server._dual_voice_arm)
        finally:
            await server._disarm_dual_voice_capture(reason="test")


if __name__ == "__main__":
    unittest.main()
