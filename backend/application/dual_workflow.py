"""Application service for the current dual-camera eight-angle workflow."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import inspect
import re
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..core.dual_capture import DualCameraCaptureCoordinator
from ..core.dual_session_store import DualSessionStore
from ..protocol import validate_subject_id


class DualCapturePersistenceError(RuntimeError):
    """A failed write with the reconciled (or conservatively locked) UI state."""

    def __init__(self, message: str, state: dict[str, Any]) -> None:
        super().__init__(message)
        self.state = state


class DualWorkflowService:
    """Own active-session identity and serialize dual workflow mutations."""

    def __init__(self, adapter_provider: Callable[[], tuple[Any, Any]]) -> None:
        self._adapter_provider = adapter_provider
        self.store: DualSessionStore | None = None
        self.active_subject_id = ""
        self._session_lock = threading.RLock()
        self._capture_in_progress = False

    def close(self) -> None:
        if self.store is not None:
            self.store.close()
        self.store = None
        self.active_subject_id = ""

    def _assert_idle(self) -> None:
        if self._capture_in_progress:
            raise ValueError("正在采集或保存，请等待完成后再切换任务或修改数据")

    def _activate_session(self, output_path: str, subject_id: str, operation) -> dict[str, Any]:
        # The caller holds _session_lock. Commit identity only after validation.
        self._assert_idle()
        resolved = Path(output_path).expanduser().resolve()
        previous = self.store
        next_store = previous if previous is not None and previous.output_directory == resolved else DualSessionStore(resolved)
        try:
            state = operation(next_store)
            result = self._public_state_from_session(state)
        except Exception:
            if next_store is not previous:
                next_store.close()
            raise
        self.store = next_store
        self.active_subject_id = subject_id
        if previous is not None and previous is not next_store:
            previous.close()
        return result

    def create_session(
        self,
        *,
        subject_id: str,
        output_path: str,
        clothing_note: str = "",
        target_distance_mm: int | None = None,
    ) -> dict[str, Any]:
        subject_id = validate_subject_id(subject_id)
        if not str(output_path or "").strip():
            raise ValueError("请选择数据输出文件夹")
        with self._session_lock:
            return self._activate_session(output_path, subject_id, lambda store: store.create_session(
                subject_id, clothing_note=clothing_note, target_distance_mm=target_distance_mm,
            ))

    def open_session(self, *, subject_id: str, output_path: str) -> dict[str, Any]:
        subject_id = validate_subject_id(subject_id)
        if not str(output_path or "").strip():
            raise ValueError("请选择原任务的数据输出文件夹")
        with self._session_lock:
            return self._activate_session(output_path, subject_id, lambda store: store.get_session(subject_id))

    def open_latest_session(self, *, output_path: str) -> dict[str, Any]:
        if not str(output_path or "").strip():
            raise ValueError("请选择数据输出文件夹")
        with self._session_lock:
            self._assert_idle()
            root = Path(output_path).expanduser().resolve()
            if not root.is_dir():
                raise ValueError("输出文件夹不存在或不可读取")
            subjects = root / "body_posture_dual_v2" / "subjects"
            candidates = []
            if subjects.exists():
                for entry in subjects.iterdir():
                    if entry.is_dir() and re.fullmatch(r"S\d{4}", entry.name):
                        candidates.append(entry.name)
            if not candidates:
                return {"found": False, "output_path": str(root)}
            subject_id = max(candidates, key=lambda value: int(value[1:]))
            # Do not silently skip a damaged newest task and select old data.
            state = self.open_session(subject_id=subject_id, output_path=str(root))
            return {"found": True, "state": state, "output_path": str(root)}

    def _active(self, subject_id: str) -> tuple[DualSessionStore, str]:
        requested = validate_subject_id(str(subject_id or "").strip().upper())
        if self.store is None or not self.active_subject_id:
            raise ValueError("请先登记或继续一个双机受试者任务")
        if requested != self.active_subject_id:
            raise ValueError("写命令中的受试者与当前活动任务不一致")
        return self.store, requested

    def public_state(self) -> dict[str, Any]:
        with self._session_lock:
            if self.store is None or not self.active_subject_id:
                return {"active": False, "angles": []}
            state = self.store.get_session(self.active_subject_id)
            return self._public_state_from_session(state)

    def _public_state_from_session(self, state: Mapping[str, Any]) -> dict[str, Any]:
        angles = sorted(
            state.get("angles", {}).values(), key=lambda item: int(item.get("yaw_deg", 0))
        )
        captured = sum(item.get("status") == "CAPTURED" for item in angles)
        anthropometry = dict(state.get("anthropometry") or {})
        blockers = []
        if captured < len(angles):
            blockers.append(f"双机八角度尚未完成（{captured}/{len(angles)}）")
        if anthropometry.get("complete") is not True:
            blockers.append("5 项必填人体测量尚未完成")
        if state.get("reconciliation_required") is True:
            blockers.append("任务存在待恢复或完整性异常")
        completed = str(state.get("status") or "").upper() == "COMPLETE"
        return {
            "active": True,
            "subject_id": state.get("subject_id", ""),
            "status": state.get("status", "ACTIVE"),
            "created_at": state.get("created_at"),
            "completed_at": state.get("completed_at"),
            "output_directory": state.get("output_directory") or str(Path(state["output_root"]).parent),
            "output_root": state.get("output_root"),
            "clothing_note": state.get("clothing_note", ""),
            "target_distance_mm": state.get("target_distance_mm"),
            "storage_features": list(state.get("storage_features") or []),
            "integrity": dict(state.get("integrity") or {}),
            "recovery_report": state.get("recovery_report"),
            "reconciliation_required": state.get("reconciliation_required") is True,
            "angles": angles,
            "progress": {
                "captured": captured,
                "expected": len(angles),
                "missing": len(angles) - captured,
                "percent": round(captured * 100.0 / len(angles), 1) if angles else 0.0,
            },
            "anthropometry": anthropometry,
            "completion": {
                **dict(state.get("completion") or {}),
                "can_complete": not blockers and not completed,
                "completed": completed,
                "completed_at": state.get("completed_at"),
                "status": "COMPLETE" if completed else "INCOMPLETE",
                "blockers": [] if completed else blockers,
            },
            "next_yaw_deg": next(
                (item.get("yaw_deg") for item in angles if item.get("status") != "CAPTURED"),
                None,
            ),
        }

    def save_anthropometry(
        self,
        *,
        subject_id: str,
        records: Sequence[Mapping[str, Any]],
        definitions: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        with self._session_lock:
            self._assert_idle()
            store, subject_id = self._active(subject_id)
            return self._public_state_from_session(store.save_anthropometry(subject_id, records, definitions))

    def complete_session(self, *, subject_id: str) -> dict[str, Any]:
        with self._session_lock:
            self._assert_idle()
            store, subject_id = self._active(subject_id)
            return self._public_state_from_session(store.complete_session(subject_id))

    async def capture_group(
        self, **kwargs,
    ) -> dict[str, Any]:
        with self._session_lock:
            self._assert_idle()
            self._capture_in_progress = True
        try:
            return await self._capture_group(**kwargs)
        finally:
            with self._session_lock:
                self._capture_in_progress = False

    async def _capture_group(
        self,
        *,
        subject_id: str,
        yaw_deg: int,
        distance_mm: int | None,
        ready: bool,
        capture_lock: asyncio.Lock,
        camera_lock: asyncio.Lock,
        set_capturing: Callable[[bool], None],
        announce: Callable[[], Any] | None = None,
        settle_seconds: float = 2.0,
        frame_count: int = 5,
        interval_ms: float = 150.0,
    ) -> dict[str, Any]:
        if not ready:
            raise ValueError("请确认受试者已按当前角度就位、Gemini 全身完整入框且两路画面稳定")
        store, subject_id = self._active(subject_id)
        state = self.public_state()
        if state.get("reconciliation_required"):
            raise ValueError("任务存在待恢复或完整性异常，禁止采集")
        if str(state.get("status") or "").upper() == "COMPLETE":
            raise ValueError("该受试者任务已完成并锁定")
        expected_yaw = state.get("next_yaw_deg")
        if expected_yaw is None:
            raise ValueError("八个角度已全部采集，禁止重复采集，请继续人体测量或完成任务")
        if expected_yaw is not None and int(yaw_deg) != int(expected_yaw):
            raise ValueError(f"请按顺序采集，下一角度为 {expected_yaw}°")
        if capture_lock.locked():
            raise ValueError("正在采集中，请稍候")
        gemini, d435i = self._adapter_provider()
        coordinator = DualCameraCaptureCoordinator(gemini, d435i)
        async with capture_lock:
            set_capturing(True)
            try:
                if announce is not None:
                    announcement = announce()
                    if inspect.isawaitable(announcement):
                        await announcement
                await asyncio.sleep(settle_seconds)
                # Preview, connect/disconnect, and a formal burst must never ask
                # either SDK for frames concurrently.
                async with camera_lock:
                    burst = await coordinator.capture_burst(
                        frame_count=frame_count,
                        interval_ms=interval_ms,
                    )
                try:
                    committed = await asyncio.to_thread(
                        store.commit_group,
                        subject_id,
                        int(yaw_deg),
                        [pair.gemini for pair in burst.pairs],
                        [pair.d435i for pair in burst.pairs],
                        audit=burst.audit_payload(),
                        metadata={
                            "distance_mm": distance_mm,
                            "ready_confirmed_at": datetime.now(timezone.utc).isoformat(),
                            "framing_policy": {
                                "C336L": "full_body_required",
                                "CD435I": "auxiliary_fov_limited_non_blocking",
                            },
                        },
                    )
                except Exception as exc:
                    # Reconcile under the capture lock before another capture can
                    # start. Never hide the original write failure if auditing fails.
                    detail = f"角度 {int(yaw_deg)}° 写入异常：{exc}"
                    try:
                        recovered_state = await asyncio.to_thread(self.public_state)
                    except Exception as recovery_exc:
                        recovered_state = {
                            **state,
                            "reconciliation_required": True,
                            "integrity": {"status": "ERROR", "errors": [
                                f"无法核验写入后的状态：{recovery_exc}；请重新打开任务后再采集。"
                            ]},
                        }
                    recovered_state["capture_error"] = detail
                    recovered_state["capture_recovered"] = (
                        not recovered_state.get("reconciliation_required")
                        and any(
                            item.get("yaw_deg") == int(yaw_deg)
                            and item.get("status") == "CAPTURED"
                            for item in recovered_state.get("angles", [])
                        )
                    )
                    raise DualCapturePersistenceError(detail, recovered_state) from exc
            finally:
                set_capturing(False)
        return {
            "success": True,
            "yaw_deg": int(yaw_deg),
            "attempt_id": committed["attempt_id"],
            "sync_audit": burst.audit_payload(),
            # commit_group already durably wrote and returned this ledger. A
            # redundant disk read here could misreport a successful commit as
            # a failed capture if a subsequent read encounters a file lock.
            "state": self._public_state_from_session(committed["state"]),
        }
