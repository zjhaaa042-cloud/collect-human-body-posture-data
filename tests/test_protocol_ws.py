import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from backend.core.camera_adapters import (
    CameraExtrinsicsData,
    CameraIntrinsicsData,
    FrameBundle,
)
from backend.core.protocol_store import ProtocolStore, ProtocolStoreError
from backend.protocol import format_condition_id, measurement_definitions, primary3
from backend.server.ws_server import WebSocketServer
from backend.voice.command_parser import VoiceCommand


class FakeProtocolCamera:
    backend = "orbbec"

    def __init__(self):
        self.counter = 0

    def get_status(self, devices=None):
        del devices
        return {
            "connected": True,
            "backend": self.backend,
            "device": {
                "camera_code": "C336L",
                "serial_number": "FAKE001",
                "name": "Orbbec Gemini 336L",
            },
        }

    def get_frames(self, timeout_ms=1000):
        self.counter += 1
        rng = np.random.default_rng(self.counter)
        color = rng.integers(50, 200, size=(16, 20, 3), dtype=np.uint8)
        depth_raw = np.zeros((12, 16), dtype=np.uint16)
        depth_raw[:, 4:12] = 2500
        depth_aligned = np.zeros((16, 20), dtype=np.uint16)
        depth_aligned[1:15, 6:14] = 2500
        infrared = {
            "left": np.full((12, 16), 60, dtype=np.uint8),
            "right": np.full((12, 16), 70, dtype=np.uint8),
        }
        timestamp = float(self.counter * 100)
        color_intrinsics = CameraIntrinsicsData(
            fx=100.0, fy=100.0, cx=10.0, cy=8.0, width=20, height=16
        )
        depth_intrinsics = CameraIntrinsicsData(
            fx=90.0, fy=90.0, cx=8.0, cy=6.0, width=16, height=12
        )
        return FrameBundle(
            color=color,
            depth_raw=depth_raw,
            depth_aligned=depth_aligned,
            infrared=infrared,
            depth_scale=1.0,
            device_timestamp=timestamp,
            frame_number=self.counter,
            camera_metadata={
                "backend": self.backend,
                "serial": "FAKE001",
                "rgb_color_order": "RGB",
                "rgb_transfer": "sRGB",
            },
            intrinsics={
                "color": color_intrinsics,
                "depth_raw": depth_intrinsics,
                "depth_aligned": color_intrinsics,
            },
            extrinsics={
                "depth_raw_to_color": CameraExtrinsicsData(
                    source="depth_raw",
                    target="color",
                    rotation=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
                    translation=(0.0, 0.0, 0.0),
                )
            },
            stream_timestamps={
                "color": timestamp,
                "depth_raw": timestamp + 0.1,
                "depth_aligned": timestamp + 0.1,
                "ir_left": timestamp + 0.1,
                "ir_right": timestamp + 0.1,
            },
            stream_frame_numbers={
                "color": self.counter,
                "depth_raw": self.counter,
                "depth_aligned": self.counter,
                "ir_left": self.counter,
                "ir_right": self.counter,
            },
        )


class ProtocolWebSocketTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        server = WebSocketServer.__new__(WebSocketServer)
        server.protocol_store = ProtocolStore(Path(self.temp.name))
        server.settings = SimpleNamespace(
            storage=SimpleNamespace(
                min_color_brightness=30,
                max_color_brightness=220,
                min_depth_coverage=0.3,
            )
        )
        server.camera = SimpleNamespace(enabled_ir_streams=["left", "right"])
        server.active_camera_adapter = FakeProtocolCamera()
        server.capture_lock = asyncio.Lock()
        server.camera_lock = asyncio.Lock()
        server.is_capturing = False
        server.voice_synthesizer = None
        server.active_protocol_subject_id = "S0001"
        server._broadcast = mock.AsyncMock()
        conditions = primary3()
        server.protocol_store.create_subject(
            "S0001",
            "RealAnthro-RGBD-v1.0",
            "primary3",
            {"operator_id": "OP01"},
            expected_conditions=[server._condition_payload(item) for item in conditions],
            capture_policy_version="realanthro-capture-v1.1",
            capture_policy=server._protocol_capture_policy(conditions),
        )
        self.server = server

    def tearDown(self):
        self.server.protocol_store.close()
        self.temp.cleanup()

    def test_catalog_defaults_to_full31_and_disables_full36(self):
        catalog = self.server._protocol_catalog()
        self.assertEqual(catalog["default_profile_id"], "full31_no_lux")
        profiles = {item["profile_id"]: item for item in catalog["profiles"]}
        self.assertEqual(profiles["full31_no_lux"]["condition_count"], 31)
        self.assertTrue(profiles["full31_no_lux"]["available"])
        self.assertFalse(profiles["full36"]["available"])

    def test_public_state_normalizes_conditions_and_progress(self):
        state = self.server._protocol_subject_state("S0001")
        self.assertIsInstance(state["conditions"], list)
        self.assertEqual(state["progress"], {
            "expected": 3,
            "captured": 0,
            "missing": 3,
            "percent": 0.0,
        })
        self.assertEqual(state["next_condition_id"], format_condition_id(primary3()[0]))
        self.assertFalse(state["completion"]["can_complete"])

    def test_preview_distance_follows_selected_condition_not_only_next(self):
        self.server.depth_analyzer = SimpleNamespace(
            target=1500.0,
            tolerance=150.0,
            reset=mock.Mock(),
        )
        selected = self.server._apply_protocol_distance_target(
            {
                "next_condition_id": "C_NEXT",
                "conditions": [
                    {"condition_id": "C_NEXT", "distance_mm": 1500},
                    {"condition_id": "C_RETAKE", "distance_mm": 3000},
                ],
            },
            "C_RETAKE",
        )
        self.assertEqual(selected["condition_id"], "C_RETAKE")
        self.assertEqual(self.server.depth_analyzer.target, 3000.0)
        self.assertEqual(self.server.depth_analyzer.tolerance, 300.0)
        self.assertEqual(self.server.depth_analyzer.min_distance, 2700.0)
        self.assertEqual(self.server.depth_analyzer.max_distance, 3300.0)
        self.server.depth_analyzer.reset.assert_called_once_with()

    def test_dual_preview_distance_uses_session_target(self):
        self.server.depth_analyzer = SimpleNamespace(
            target=1500.0,
            tolerance=150.0,
            reset=mock.Mock(),
        )
        self.server._apply_dual_distance_target({"target_distance_mm": 2500})
        self.assertEqual(self.server.depth_analyzer.target, 2500.0)
        self.assertEqual(self.server.depth_analyzer.tolerance, 250.0)
        self.assertEqual(self.server.depth_analyzer.min_distance, 2250.0)
        self.assertEqual(self.server.depth_analyzer.max_distance, 2750.0)
        self.server.depth_analyzer.reset.assert_called_once_with()

    def test_dual_preview_without_session_target_clears_previous_target(self):
        for state in ({}, {"target_distance_mm": None}, {"target_distance_mm": ""}):
            with self.subTest(state=state):
                self.server.depth_analyzer = SimpleNamespace(
                    target=1500.0,
                    tolerance=150.0,
                    reset=mock.Mock(),
                )
                self.server._apply_dual_distance_target({"target_distance_mm": 4000})
                self.server.depth_analyzer.reset.reset_mock()
                self.server._apply_dual_distance_target(state)
                self.assertEqual(self.server.depth_analyzer.target, 2500.0)
                self.assertEqual(self.server.depth_analyzer.tolerance, 250.0)
                self.assertEqual(self.server.depth_analyzer.min_distance, 2250.0)
                self.assertEqual(self.server.depth_analyzer.max_distance, 2750.0)
                self.server.depth_analyzer.reset.assert_called_once_with()

    def test_old_capture_policy_can_be_closed_but_cannot_add_images(self):
        condition_item = primary3()[0]
        self.server.protocol_store.create_subject(
            "SOLD01",
            "RealAnthro-RGBD-v1.0",
            "legacy_policy",
            {"operator_id": "OP01"},
            expected_conditions=[self.server._condition_payload(condition_item)],
            capture_policy_version="realanthro-capture-v1.0",
        )

        self.server._assert_protocol_subject_writable("SOLD01")
        with self.assertRaisesRegex(
            ProtocolStoreError,
            "缺少跨条件相机指纹门禁",
        ):
            self.server._assert_protocol_subject_writable(
                "SOLD01",
                require_camera_fingerprint=True,
            )

    async def test_preview_loop_recovers_after_transient_frame_error(self):
        server = WebSocketServer.__new__(WebSocketServer)
        server.is_previewing = True
        server.is_capturing = False
        server.clients = {object()}
        server.camera_lock = asyncio.Lock()
        server.settings = SimpleNamespace(gui=SimpleNamespace(preview_fps=30))
        server._broadcast = mock.AsyncMock()
        server._update_auto_capture = mock.AsyncMock()

        class FlakyPreviewCamera:
            last_error = ""

            def __init__(self):
                self.calls = 0

            def get_frames(self, timeout_ms=1000):
                del timeout_ms
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("temporary frame failure")
                server.is_previewing = False
                return None

        camera = FlakyPreviewCamera()
        server.active_camera_adapter = camera
        with mock.patch(
            "backend.server.ws_server.asyncio.sleep", new=mock.AsyncMock()
        ):
            await server._preview_loop()

        self.assertEqual(camera.calls, 2)
        recovery_messages = [
            call.args[0]
            for call in server._broadcast.await_args_list
            if call.args[0].get("data", {}).get("distance", {}).get("message")
            == "预览暂时中断，正在自动重试"
        ]
        self.assertEqual(len(recovery_messages), 1)

    async def test_ensure_preview_task_reuses_running_task(self):
        server = WebSocketServer.__new__(WebSocketServer)
        server.is_previewing = False
        running = asyncio.create_task(asyncio.sleep(30))
        server.preview_task = running
        try:
            first = await server._ensure_preview_task()
            second = await server._ensure_preview_task()
            self.assertIs(first, running)
            self.assertIs(second, running)
            self.assertTrue(server.is_previewing)
        finally:
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running

    async def test_slow_camera_disconnect_does_not_block_event_loop(self):
        server = WebSocketServer.__new__(WebSocketServer)
        server.is_shutting_down = False
        server.capture_lock = asyncio.Lock()
        server.camera_lock = asyncio.Lock()
        server.camera_operation_lock = asyncio.Lock()
        server.depth_analyzer = SimpleNamespace(reset=mock.Mock())
        server._broadcast = mock.AsyncMock()

        class SlowCamera:
            backend = "orbbec"

            def __init__(self):
                self.connected = True

            def disconnect(self):
                time.sleep(0.15)
                self.connected = False

            def get_status(self, devices=None):
                del devices
                return {
                    "connected": self.connected,
                    "device": {"name": "Fake camera"},
                    "message": "ok",
                }

        camera = SlowCamera()
        server.active_camera_adapter = camera
        server.camera_registry = SimpleNamespace(
            list_devices=lambda: [],
            adapters={"orbbec": camera},
        )

        disconnected = asyncio.create_task(server._handle_disconnect_camera())
        await asyncio.sleep(0.03)
        self.assertFalse(disconnected.done())
        self.assertGreaterEqual(server._broadcast.await_count, 1)
        await disconnected
        self.assertFalse(camera.connected)

    async def test_disconnect_announces_transition_before_camera_lock_is_free(self):
        server = WebSocketServer.__new__(WebSocketServer)
        server.is_shutting_down = False
        server.capture_lock = asyncio.Lock()
        server.camera_lock = asyncio.Lock()
        server.camera_operation_lock = asyncio.Lock()
        server.depth_analyzer = SimpleNamespace(reset=mock.Mock())
        server._broadcast = mock.AsyncMock()

        class Camera:
            backend = "orbbec"

            def __init__(self):
                self.connected = True

            def disconnect(self):
                self.connected = False

            def get_status(self, devices=None):
                del devices
                return {"connected": self.connected, "device": {}, "message": "ok"}

        camera = Camera()
        server.active_camera_adapter = camera
        server.camera_registry = SimpleNamespace(
            list_devices=lambda: [],
            adapters={"orbbec": camera},
        )

        await server.camera_lock.acquire()
        task = asyncio.create_task(server._handle_disconnect_camera())
        await asyncio.sleep(0)
        first_message = server._broadcast.await_args_list[0].args[0]
        self.assertEqual(first_message, {
            "type": "camera_operation",
            "data": {"state": "disconnecting"},
        })
        self.assertFalse(task.done())
        self.assertTrue(camera.connected)

        server.camera_lock.release()
        await task
        message_types = [
            call.args[0]["type"] for call in server._broadcast.await_args_list
        ]
        self.assertEqual(
            message_types,
            ["camera_operation", "preview_frame", "camera_status"],
        )

    async def test_stop_preview_drains_inflight_frame_before_releasing_lock(self):
        server = WebSocketServer.__new__(WebSocketServer)
        server.is_previewing = True
        server.is_capturing = False
        server.clients = {object()}
        server.camera_lock = asyncio.Lock()
        server.settings = SimpleNamespace(gui=SimpleNamespace(preview_fps=30))
        server._broadcast = mock.AsyncMock()
        server._update_auto_capture = mock.AsyncMock()
        frame_started = threading.Event()
        release_frame = threading.Event()

        class BlockingCamera:
            last_error = ""

            def get_frames(self, timeout_ms=1000):
                del timeout_ms
                frame_started.set()
                release_frame.wait(1)
                return None

        server.active_camera_adapter = BlockingCamera()
        server.preview_task = asyncio.create_task(server._preview_loop())
        await asyncio.to_thread(frame_started.wait, 1)

        stop_task = asyncio.create_task(server._stop_preview())
        await asyncio.sleep(0.02)
        self.assertFalse(stop_task.done())
        self.assertTrue(server.camera_lock.locked())

        release_frame.set()
        await stop_task
        self.assertFalse(server.camera_lock.locked())
        self.assertIsNone(server.preview_task)

    async def test_legacy_capture_commands_are_read_only(self):
        websocket = SimpleNamespace(send=mock.AsyncMock())
        self.server._handle_capture = mock.AsyncMock()
        self.server._start_auto_capture = mock.AsyncMock()
        self.server._handle_finish = mock.AsyncMock()
        self.server.data_collector = SimpleNamespace(
            create_session=mock.Mock(),
            update_capture_review=mock.Mock(),
        )

        await self.server._process_message(websocket, {"type": "capture_single"})
        await self.server._process_message(websocket, {"type": "start_auto_capture"})
        self.server.active_protocol_subject_id = None
        await self.server._process_message(websocket, {"type": "create_session"})
        await self.server._process_message(websocket, {"type": "review_capture"})
        await self.server._process_message(websocket, {"type": "finish_session"})

        self.server._handle_capture.assert_not_awaited()
        self.server._start_auto_capture.assert_not_awaited()
        self.server._handle_finish.assert_not_awaited()
        self.server.data_collector.create_session.assert_not_called()
        self.server.data_collector.update_capture_review.assert_not_called()
        messages = [call.args[0] for call in websocket.send.await_args_list]
        self.assertTrue(any("旧版单帧采集已停用" in item for item in messages))
        self.assertTrue(any("旧版自动连拍已停用" in item for item in messages))
        self.assertTrue(any("旧版会话创建已停用" in item for item in messages))
        self.assertTrue(any("旧版样本复核写入已停用" in item for item in messages))
        self.assertTrue(any("旧版会话完成写入已停用" in item for item in messages))

    async def test_legacy_method_boundaries_cannot_write(self):
        collector = SimpleNamespace(
            capture=mock.Mock(),
            close_session=mock.Mock(),
        )
        self.server.data_collector = collector
        self.server._broadcast_auto_status = mock.AsyncMock()
        self.server.auto_capture_enabled = True

        started = await self.server._start_auto_capture({})
        captured = await self.server._handle_capture({})
        finished = await self.server._handle_finish()

        self.assertFalse(started)
        self.assertFalse(self.server.auto_capture_enabled)
        self.assertIsNone(captured)
        self.assertFalse(finished)
        collector.capture.assert_not_called()
        collector.close_session.assert_not_called()

    async def test_protocol_notification_failure_does_not_escape(self):
        websocket = SimpleNamespace(
            send=mock.AsyncMock(side_effect=RuntimeError("slow client"))
        )

        await self.server._emit_protocol_message(
            websocket,
            {"type": "protocol_subject_state", "data": {}},
        )

        websocket.send.assert_awaited_once()

    async def test_idle_shutdown_waits_for_first_authenticated_client(self):
        server = WebSocketServer.__new__(WebSocketServer)
        server.clients = set()
        server.is_shutting_down = False
        server._had_authenticated_client = False
        with mock.patch(
            "backend.server.ws_server.asyncio.sleep",
            new=mock.AsyncMock(),
        ):
            await server._shutdown_after_idle(delay=0)
        self.assertFalse(server.is_shutting_down)

    def test_voice_stop_does_not_cancel_protocol_transaction(self):
        server = WebSocketServer.__new__(WebSocketServer)
        server.voice_synthesizer = None
        server.voice_parser = SimpleNamespace(
            execute_command=mock.Mock(return_value=VoiceCommand.STOP_CAPTURE)
        )
        server._last_voice_command = None
        server._last_voice_command_at = 0.0
        server.is_capturing = True

        server._on_voice_command("停止")

        self.assertTrue(server.is_capturing)

    async def test_capture_enforces_next_condition_and_commits_five_frames(self):
        conditions = primary3()
        with self.assertRaisesRegex(ValueError, "下一条件"):
            await self.server._capture_protocol_condition(
                None,
                {
                    "condition_id": format_condition_id(conditions[1]),
                    "confirmations": {
                        "distance_marker": True,
                        "pose_view_clothing": True,
                        "full_body_visible": True,
                        "repositioned": True,
                    },
                },
            )

        current = self.server._protocol_subject_state("S0001")
        nonce = current["conditions"][0]["confirmation_nonce"]
        with mock.patch("backend.server.ws_server.asyncio.sleep", new=mock.AsyncMock()):
            result = await self.server._capture_protocol_condition(
                None,
                {
                    "condition_id": format_condition_id(conditions[0]),
                    "confirmations": {
                        "distance_marker": True,
                        "pose_view_clothing": True,
                        "full_body_visible": True,
                        "nonce": nonce,
                    },
                },
            )
        self.assertTrue(result["success"])
        self.assertEqual(result["quality_status"], "WARN")
        self.assertTrue(result["review_required"])
        self.assertTrue(result["review_preview"]["color"])
        raw = self.server.protocol_store.get_subject_state("S0001")
        condition_state = raw["conditions"][format_condition_id(conditions[0])]
        self.assertEqual(condition_state["status"], "REVIEW_REQUIRED")
        attempt = raw["attempts"][result["attempt_id"]]
        self.assertEqual(len(attempt["frames"]), 5)
        self.assertEqual(len(attempt["files"]), 35)

        preview = self.server._load_protocol_review_preview(
            "S0001", format_condition_id(conditions[0]), result["attempt_id"]
        )
        self.assertTrue(preview["color"])
        self.assertTrue(preview["depth"])
        with self.assertRaisesRegex(ValueError, "落盘的 F03"):
            await self.server._review_protocol_capture(None, {
                "condition_id": format_condition_id(conditions[0]),
                "attempt_id": result["attempt_id"],
                "decision": "ACCEPT",
                "reason": "没有读取落盘证据时不得接受",
            })
        token_record = self.server._protocol_review_evidence_tokens[
            ("S0001", result["attempt_id"])
        ]
        token_record["evidence_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "发生变化"):
            await self.server._review_protocol_capture(None, {
                "condition_id": format_condition_id(conditions[0]),
                "attempt_id": result["attempt_id"],
                "decision": "ACCEPT",
                "reason": "证据摘要不匹配时必须重新读取",
                "evidence_token": preview["evidence_token"],
            })
        preview = self.server._load_protocol_review_preview(
            "S0001", format_condition_id(conditions[0]), result["attempt_id"]
        )
        review = await self.server._review_protocol_capture(None, {
            "condition_id": format_condition_id(conditions[0]),
            "attempt_id": result["attempt_id"],
            "decision": "ACCEPT",
            "reason": "F03 人体、全身和标签均已人工复核",
            "evidence_token": preview["evidence_token"],
        })
        self.assertTrue(review["success"])
        raw = self.server.protocol_store.get_subject_state("S0001")
        condition_state = raw["conditions"][format_condition_id(conditions[0])]
        self.assertEqual(condition_state["status"], "CAPTURED")
        self.assertEqual(condition_state["accepted_attempt_id"], result["attempt_id"])

    async def test_production_strict_policy_accepts_server_generated_qc(self):
        conditions = primary3()
        self.server.protocol_store.create_subject(
            "S0002",
            "RealAnthro-RGBD-v1.0",
            "primary3",
            {"operator_id": "OP01"},
            expected_conditions=[
                self.server._condition_payload(item) for item in conditions
            ],
            capture_policy_version="realanthro-capture-v1.1",
            capture_policy=self.server._protocol_capture_policy(conditions),
        )
        self.server.active_protocol_subject_id = "S0002"
        # A later settings change must not rewrite this subject's frozen QC.
        self.server.settings.storage.min_color_brightness = 250
        self.server.settings.storage.min_depth_coverage = 0.95
        state = self.server._protocol_subject_state("S0002")
        nonce = state["conditions"][0]["confirmation_nonce"]
        with mock.patch("backend.server.ws_server.asyncio.sleep", new=mock.AsyncMock()):
            result = await self.server._capture_protocol_condition(
                None,
                {
                    "subject_id": "S0002",
                    "condition_id": format_condition_id(conditions[0]),
                    "confirmations": {
                        "distance_marker": True,
                        "pose_view_clothing": True,
                        "full_body_visible": True,
                        "nonce": nonce,
                    },
                },
            )
        self.assertTrue(result["committed"])
        self.assertEqual(result["quality_status"], "WARN")
        raw = self.server.protocol_store.get_subject_state("S0002")
        attempt = raw["attempts"][result["attempt_id"]]
        self.assertEqual(attempt["status"], "COMMITTED")
        self.assertEqual(attempt["review_status"], "PENDING")

    async def test_pending_reconcile_is_reported_as_durable_and_blocks_writes(self):
        condition = primary3()[0]
        state = self.server._protocol_subject_state("S0001")
        nonce = state["conditions"][0]["confirmation_nonce"]
        pending_result = {
            "attempt_id": "AT_FAULT_INJECTION",
            "quality_status": "WARN",
            "bookkeeping_status": "PENDING_RECONCILE",
            "post_commit_error": "injected state write failure",
            "recovery_error": "injected recovery failure",
        }
        with (
            mock.patch("backend.server.ws_server.asyncio.sleep", new=mock.AsyncMock()),
            mock.patch.object(
                self.server.protocol_store,
                "commit_capture_attempt",
                return_value=pending_result,
            ),
        ):
            result = await self.server._capture_protocol_condition(None, {
                "condition_id": format_condition_id(condition),
                "confirmations": {
                    "distance_marker": True,
                    "pose_view_clothing": True,
                    "full_body_visible": True,
                    "nonce": nonce,
                },
            })
        self.assertTrue(result["committed"])
        self.assertFalse(result["success"])
        self.assertTrue(result["reconciliation_required"])
        self.assertIsNone(result["state"])
        with self.assertRaisesRegex(Exception, "待恢复"):
            await self.server._save_protocol_anthropometry(None, {
                "records": [],
                "equipment": {},
            })

    async def test_anthropometry_records_round_trip_through_ws_adapter(self):
        records = []
        for definition in measurement_definitions():
            if not definition.required:
                continue
            for field_name in definition.field_names:
                base_value = 170.0 if definition.measurement_id == "M01" else 70.0
                records.append({
                    "measurement_id": definition.measurement_id,
                    "field_name": field_name,
                    "m1": base_value,
                    "m2": base_value + 0.2,
                })
        await self.server._save_protocol_anthropometry(None, {
            "records": records,
            "equipment": {},
        })
        state = self.server._protocol_subject_state("S0001")
        self.assertTrue(state["anthropometry"]["complete"])
        self.assertEqual(len(state["anthropometry"]["records"]), 5)

    async def test_subject_creation_allows_omitting_operator_id(self):
        state = await self.server._create_protocol_subject(None, {
            "subject_id": "S0002",
            "profile_id": "primary3",
            "metadata": {"consent_internal": True},
        })
        self.assertEqual(state["subject_id"], "S0002")
        self.assertEqual(state["subject_metadata"]["operator_id"], "")

    async def test_daily_equipment_check_can_be_referenced_by_anthropometry(self):
        equipment = {
            "stadiometer_id": "HEIGHT01",
            "scale_id": "SCALE01",
            "tape_id": "TAPE01",
            "anthropometer_id": "ANTHRO01",
            "equipment_check_confirmed": True,
        }
        daily = await self.server._save_daily_equipment_check(None, {
            "subject_id": "S0001", "equipment": equipment,
        })
        self.assertEqual(
            self.server._protocol_subject_state("S0001")["daily_equipment_check"]["check_id"],
            daily["check"]["check_id"],
        )
        records = []
        for definition in measurement_definitions():
            if definition.required:
                for field_name in definition.field_names:
                    records.append({
                        "measurement_id": definition.measurement_id,
                        "field_name": field_name,
                        "m1": 170.0 if definition.measurement_id == "M01" else 70.0,
                        "m2": 170.2 if definition.measurement_id == "M01" else 70.2,
                    })
        await self.server._save_protocol_anthropometry(None, {
            "records": records,
            "equipment": {"daily_check_id": daily["check"]["check_id"]},
        })
        state = self.server._protocol_subject_state("S0001")
        self.assertEqual(
            state["anthropometry"]["metadata"]["equipment"]["daily_check_id"],
            daily["check"]["check_id"],
        )


if __name__ == "__main__":
    unittest.main()
