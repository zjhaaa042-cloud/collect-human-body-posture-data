import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from backend.application.dual_workflow import DualCapturePersistenceError, DualWorkflowService
from backend.core.camera_adapters import (
    CameraExtrinsicsData,
    CameraIntrinsicsData,
    FrameBundle,
)


class FakeCamera:
    def __init__(self, camera_code, lock):
        self.camera_code = camera_code
        self.lock = lock
        self.calls = 0
        self.observed_locked = []

    def get_status(self):
        return {"connected": True, "device": {"camera_code": self.camera_code}}

    def get_frames(self, timeout_ms):
        del timeout_ms
        self.observed_locked.append(self.lock.locked())
        self.calls += 1
        intrinsic = CameraIntrinsicsData(
            fx=2.0, fy=2.0, cx=0.5, cy=0.5, width=2, height=2,
        )
        return FrameBundle(
            color=np.zeros((2, 2, 3), dtype=np.uint8),
            depth_raw=np.full((2, 2), 1000, dtype=np.uint16),
            depth_aligned=np.full((2, 2), 1000, dtype=np.uint16),
            camera_metadata={"rgb_color_order": "RGB"},
            intrinsics={
                "color": intrinsic,
                "depth_raw": intrinsic,
                "depth_aligned": intrinsic,
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
                "color": float(self.calls),
                "depth_raw": float(self.calls),
                "depth_aligned": float(self.calls),
            },
            stream_frame_numbers={
                "color": self.calls,
                "depth_raw": self.calls,
                "depth_aligned": self.calls,
            },
            host_timestamp_ns=1_000_000_000 + self.calls * 1_000_000,
        )


class DualWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_rejects_switch_and_returns_original_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            camera_lock = asyncio.Lock()
            service = DualWorkflowService(lambda: (FakeCamera("C336L", camera_lock), FakeCamera("CD435I", camera_lock)))
            service.create_session(subject_id="S0001", output_path=directory)
            entered, release = asyncio.Event(), asyncio.Event()

            async def announce():
                entered.set()
                await release.wait()

            task = asyncio.create_task(service.capture_group(
                subject_id="S0001", yaw_deg=0, distance_mm=2500, ready=True,
                capture_lock=asyncio.Lock(), camera_lock=camera_lock, set_capturing=lambda _: None,
                announce=announce, settle_seconds=0, interval_ms=0,
            ))
            await entered.wait()
            try:
                for operation in (
                    lambda: service.create_session(subject_id="S0002", output_path=directory),
                    lambda: service.open_session(subject_id="S0001", output_path=directory),
                    lambda: service.open_latest_session(output_path=directory),
                ):
                    with self.assertRaisesRegex(ValueError, "正在采集"):
                        await asyncio.to_thread(operation)
            finally:
                release.set()
                result = await task
            self.assertEqual(result["state"]["subject_id"], "S0001")
            self.assertEqual(result["state"]["progress"]["captured"], 1)
            self.assertFalse((service.store.root / "subjects" / "S0002").exists())
            service.create_session(subject_id="S0002", output_path=directory)
            service.close()

    async def test_failed_open_and_create_preserve_original_store_and_subject(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            service = DualWorkflowService(lambda: (None, None))
            other = DualWorkflowService(lambda: (None, None))
            service.create_session(subject_id="S0001", output_path=first)
            other.create_session(subject_id="S0002", output_path=second)
            other.close()
            original = service.store
            for operation in (
                lambda: service.open_session(subject_id="S0999", output_path=second),
                lambda: service.create_session(subject_id="S0002", output_path=second),
            ):
                with self.assertRaises(Exception):
                    operation()
                self.assertIs(service.store, original)
                self.assertEqual(service.public_state()["subject_id"], "S0001")
                self.assertEqual(service.public_state()["output_directory"], first)
            service.close()

    async def test_latest_uses_highest_id_restores_progress_and_preserves_on_error(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as empty:
            camera_lock = asyncio.Lock()
            service = DualWorkflowService(lambda: (FakeCamera("C336L", camera_lock), FakeCamera("CD435I", camera_lock)))
            service.create_session(subject_id="S0026", output_path=directory)
            await service.capture_group(
                subject_id="S0026", yaw_deg=0, distance_mm=2500, ready=True,
                capture_lock=asyncio.Lock(), camera_lock=camera_lock, set_capturing=lambda _: None,
                settle_seconds=0, interval_ms=0,
            )
            service.create_session(subject_id="S0002", output_path=directory)
            result = service.open_latest_session(output_path=directory)
            self.assertTrue(result["found"])
            self.assertEqual(result["state"]["subject_id"], "S0026")
            self.assertEqual(result["state"]["progress"]["captured"], 1)
            self.assertEqual(result["state"]["next_yaw_deg"], 45)
            original = service.store
            self.assertFalse(service.open_latest_session(output_path=empty)["found"])
            self.assertFalse((Path(empty) / "body_posture_dual_v2").exists())
            (service.store.root / "subjects" / "S0099").mkdir()
            with self.assertRaisesRegex(Exception, "未找到"):
                service.open_latest_session(output_path=directory)
            self.assertIs(service.store, original)
            self.assertEqual(service.active_subject_id, "S0026")
            service.close()

    async def test_ninth_capture_is_rejected_before_camera_access(self):
        with tempfile.TemporaryDirectory() as directory:
            camera_lock = asyncio.Lock()
            gemini, d435i = FakeCamera("C336L", camera_lock), FakeCamera("CD435I", camera_lock)
            service = DualWorkflowService(lambda: (gemini, d435i))
            service.create_session(subject_id="S0001", output_path=directory)
            options = dict(subject_id="S0001", distance_mm=2500, ready=True,
                           capture_lock=asyncio.Lock(), camera_lock=camera_lock,
                           set_capturing=lambda _: None, settle_seconds=0, interval_ms=0)
            for yaw in range(0, 360, 45):
                await service.capture_group(yaw_deg=yaw, **options)
            calls = gemini.calls + d435i.calls
            with self.assertRaisesRegex(ValueError, "全部采集"):
                await service.capture_group(yaw_deg=0, **options)
            self.assertEqual(gemini.calls + d435i.calls, calls)
            self.assertEqual(len(service.store.get_session("S0001")["angles"]["V000"]["attempts"]), 1)
            service.close()

    async def test_success_uses_committed_ledger_without_post_commit_disk_read(self):
        with tempfile.TemporaryDirectory() as directory:
            camera_lock = asyncio.Lock()
            service = DualWorkflowService(lambda: (
                FakeCamera("C336L", camera_lock), FakeCamera("CD435I", camera_lock)
            ))
            service.create_session(subject_id="S0001", output_path=directory)
            initial = service.public_state()
            try:
                with mock.patch.object(service, "public_state", side_effect=[initial, OSError("read locked")]) as read:
                    result = await service.capture_group(
                        subject_id="S0001", yaw_deg=0, distance_mm=2500, ready=True,
                        capture_lock=asyncio.Lock(), camera_lock=camera_lock,
                        set_capturing=lambda value: None, settle_seconds=0, interval_ms=0,
                    )
                self.assertTrue(result["success"])
                self.assertEqual(result["state"]["progress"]["captured"], 1)
                self.assertEqual(result["state"]["next_yaw_deg"], 45)
                read.assert_called_once()
            finally:
                service.close()

    async def test_write_failure_returns_recovered_or_locked_state(self):
        for failure in ("ledger", "partial", "audit"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                camera_lock = asyncio.Lock()
                capture_lock = asyncio.Lock()
                service = DualWorkflowService(lambda: (
                    FakeCamera("C336L", camera_lock), FakeCamera("CD435I", camera_lock)
                ))
                service.create_session(subject_id="S0001", output_path=directory)
                store = service.store
                original = store._atomic_json
                ledger_failed = False

                def fail_ledger_once(path, value):
                    nonlocal ledger_failed
                    if path == store._state_path("S0001") and not ledger_failed:
                        ledger_failed = True
                        raise PermissionError("injected ledger lock")
                    return original(path, value)

                initial_state = service.public_state()
                if failure == "ledger":
                    injection = mock.patch.object(store, "_atomic_json", side_effect=fail_ledger_once)
                else:
                    injection = mock.patch.object(store, "_write_png", side_effect=OSError("injected disk write failure"))
                audit = (
                    mock.patch.object(service, "public_state", side_effect=[initial_state, OSError("audit unavailable")])
                    if failure == "audit" else mock.patch.object(service, "public_state", wraps=service.public_state)
                )
                capturing = []
                try:
                    with injection, audit, self.assertRaises(DualCapturePersistenceError) as caught:
                        await service.capture_group(
                            subject_id="S0001", yaw_deg=0, distance_mm=2500, ready=True,
                            capture_lock=capture_lock, camera_lock=camera_lock,
                            set_capturing=capturing.append, settle_seconds=0, interval_ms=0,
                        )
                    state = caught.exception.state
                    self.assertIn("写入异常", state["capture_error"])
                    self.assertEqual(state["capture_recovered"], failure == "ledger")
                    self.assertEqual(state["reconciliation_required"], failure != "ledger")
                    self.assertEqual(state["progress"]["captured"], 1 if failure == "ledger" else 0)
                    self.assertEqual(capturing, [True, False])
                    self.assertFalse(capture_lock.locked())
                    self.assertFalse(camera_lock.locked())
                    if failure == "ledger":
                        self.assertEqual(state["next_yaw_deg"], 45)
                    else:
                        self.assertTrue(list((store._subject_dir("S0001") / ".staging").iterdir()))
                finally:
                    service.close()

    async def test_write_subject_must_match_active_session(self):
        with tempfile.TemporaryDirectory() as directory:
            service = DualWorkflowService(lambda: (None, None))
            service.create_session(
                subject_id="S0001", output_path=directory,
            )
            with self.assertRaisesRegex(ValueError, "当前活动任务不一致"):
                service.complete_session(subject_id="S0002")
            service.close()

    async def test_formal_burst_holds_camera_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            camera_lock = asyncio.Lock()
            gemini = FakeCamera("C336L", camera_lock)
            d435i = FakeCamera("CD435I", camera_lock)
            service = DualWorkflowService(lambda: (gemini, d435i))
            service.create_session(subject_id="S0001", output_path=directory)
            committed = {
                "attempt_id": "capture_test",
                "state": service.store.get_session("S0001"),
                "capture": {},
            }
            with mock.patch.object(
                service.store, "commit_group", return_value=committed
            ) as commit_group:
                result = await service.capture_group(
                    subject_id="S0001",
                    yaw_deg=0,
                    distance_mm=2500,
                    ready=True,
                    capture_lock=asyncio.Lock(),
                    camera_lock=camera_lock,
                    set_capturing=lambda value: None,
                    settle_seconds=0,
                    interval_ms=0,
                )
            self.assertEqual(result["attempt_id"], "capture_test")
            self.assertEqual(gemini.calls, 5)
            self.assertEqual(d435i.calls, 5)
            self.assertTrue(all(gemini.observed_locked + d435i.observed_locked))
            metadata = commit_group.call_args.kwargs["metadata"]
            self.assertEqual(metadata["framing_policy"], {
                "C336L": "full_body_required",
                "CD435I": "auxiliary_fov_limited_non_blocking",
            })
            service.close()

    async def test_capture_waits_for_async_announcement(self):
        with tempfile.TemporaryDirectory() as directory:
            camera_lock = asyncio.Lock()
            gemini = FakeCamera("C336L", camera_lock)
            d435i = FakeCamera("CD435I", camera_lock)
            service = DualWorkflowService(lambda: (gemini, d435i))
            service.create_session(subject_id="S0001", output_path=directory)
            announcement_started = asyncio.Event()
            release_announcement = asyncio.Event()

            async def announce():
                announcement_started.set()
                await release_announcement.wait()

            committed = {"attempt_id": "capture_test", "state": service.store.get_session("S0001"), "capture": {}}
            with mock.patch.object(service.store, "commit_group", return_value=committed):
                task = asyncio.create_task(service.capture_group(
                    subject_id="S0001",
                    yaw_deg=0,
                    distance_mm=2500,
                    ready=True,
                    capture_lock=asyncio.Lock(),
                    camera_lock=camera_lock,
                    set_capturing=lambda value: None,
                    announce=announce,
                    settle_seconds=0,
                    interval_ms=0,
                ))
                await announcement_started.wait()
                self.assertEqual(gemini.calls + d435i.calls, 0)
                release_announcement.set()
                await task
            self.assertEqual(gemini.calls, 5)
            self.assertEqual(d435i.calls, 5)
            service.close()

    async def test_capture_keeps_two_second_settle_when_announcement_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            camera_lock = asyncio.Lock()
            gemini = FakeCamera("C336L", camera_lock)
            d435i = FakeCamera("CD435I", camera_lock)
            service = DualWorkflowService(lambda: (gemini, d435i))
            service.create_session(subject_id="S0001", output_path=directory)
            observed_before_settle = []

            async def announce():
                return False

            async def fake_sleep(seconds):
                observed_before_settle.append((seconds, gemini.calls, d435i.calls))

            committed = {"attempt_id": "capture_test", "state": service.store.get_session("S0001"), "capture": {}}
            with (
                mock.patch.object(service.store, "commit_group", return_value=committed),
                mock.patch(
                    "backend.application.dual_workflow.asyncio.sleep",
                    side_effect=fake_sleep,
                ) as sleep_mock,
            ):
                await service.capture_group(
                    subject_id="S0001",
                    yaw_deg=0,
                    distance_mm=2500,
                    ready=True,
                    capture_lock=asyncio.Lock(),
                    camera_lock=camera_lock,
                    set_capturing=lambda value: None,
                    announce=announce,
                    settle_seconds=2,
                    interval_ms=0,
                )
            self.assertEqual(sleep_mock.await_args_list[0], mock.call(2))
            self.assertEqual(observed_before_settle[0], (2, 0, 0))
            self.assertEqual((gemini.calls, d435i.calls), (5, 5))
            service.close()


if __name__ == "__main__":
    unittest.main()
