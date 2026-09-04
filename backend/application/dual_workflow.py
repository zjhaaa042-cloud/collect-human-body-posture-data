"""Application service for the current dual-camera eight-angle workflow."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..core.dual_capture import DualCameraCaptureCoordinator
from ..core.dual_session_store import DualSessionStore
from ..protocol import validate_subject_id


class DualWorkflowService:
    """Own active-session identity and serialize dual workflow mutations."""

    def __init__(self, adapter_provider: Callable[[], tuple[Any, Any]]) -> None:
        self._adapter_provider = adapter_provider
        self.store: DualSessionStore | None = None
        self.active_subject_id = ""

    def close(self) -> None:
        if self.store is not None:
            self.store.close()
        self.store = None
        self.active_subject_id = ""

    def _select_store(self, output_path: str) -> DualSessionStore:
        resolved = Path(output_path).expanduser().resolve()
        if self.store is not None and self.store.output_directory == resolved:
            return self.store
        next_store = DualSessionStore(resolved)
        previous = self.store
        self.store = next_store
        if previous is not None:
            previous.close()
        return next_store

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
        store = self._select_store(output_path)
        store.create_session(
            subject_id,
            clothing_note=clothing_note,
            target_distance_mm=target_distance_mm,
        )
        self.active_subject_id = subject_id
        return self.public_state()

    def open_session(self, *, subject_id: str, output_path: str) -> dict[str, Any]:
        subject_id = validate_subject_id(subject_id)
        if not str(output_path or "").strip():
            raise ValueError("请选择原任务的数据输出文件夹")
        store = self._select_store(output_path)
        store.get_session(subject_id)
        self.active_subject_id = subject_id
        return self.public_state()

    def _active(self, subject_id: str) -> tuple[DualSessionStore, str]:
        requested = validate_subject_id(str(subject_id or "").strip().upper())
        if self.store is None or not self.active_subject_id:
            raise ValueError("请先登记或继续一个双机受试者任务")
        if requested != self.active_subject_id:
            raise ValueError("写命令中的受试者与当前活动任务不一致")
        return self.store, requested

    def public_state(self) -> dict[str, Any]:
        if self.store is None or not self.active_subject_id:
            return {"active": False, "angles": []}
        state = self.store.get_session(self.active_subject_id)
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
            "subject_id": self.active_subject_id,
            "status": state.get("status", "ACTIVE"),
            "created_at": state.get("created_at"),
            "completed_at": state.get("completed_at"),
            "output_directory": state.get("output_directory") or str(self.store.output_directory),
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
        store, subject_id = self._active(subject_id)
        store.save_anthropometry(subject_id, records, definitions)
        return self.public_state()

    def complete_session(self, *, subject_id: str) -> dict[str, Any]:
        store, subject_id = self._active(subject_id)
        store.complete_session(subject_id)
        return self.public_state()

    async def capture_group(
        self,
        *,
        subject_id: str,
        yaw_deg: int,
        distance_mm: int | None,
        ready: bool,
        capture_lock: asyncio.Lock,
        camera_lock: asyncio.Lock,
        set_capturing: Callable[[bool], None],
        announce: Callable[[], None] | None = None,
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
                    announce()
                await asyncio.sleep(settle_seconds)
                # Preview, connect/disconnect, and a formal burst must never ask
                # either SDK for frames concurrently.
                async with camera_lock:
                    burst = await coordinator.capture_burst(
                        frame_count=frame_count,
                        interval_ms=interval_ms,
                    )
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
            finally:
                set_capturing(False)
        return {
            "success": True,
            "yaw_deg": int(yaw_deg),
            "attempt_id": committed["attempt_id"],
            "sync_audit": burst.audit_payload(),
            "state": self.public_state(),
        }
