"""Near-synchronous acquisition for one Gemini 336L and one D435i.

This coordinator deliberately reports *host-clock near synchrony*, not
hardware-trigger synchrony.  The two SDKs have independent device clocks and
USB queues, so callers must retain the reported skew and must not fuse point
clouds without a separately validated cross-camera calibration.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Mapping

from .camera_adapters import CameraAdapter, FrameBundle
from .frame_contract import FrameContractError, validate_frame_contract


class DualCameraCaptureError(RuntimeError):
    """Raised when a near-synchronous dual-camera pair cannot be acquired."""


@dataclass(frozen=True)
class DualFramePair:
    """One best-effort concurrent frame pair plus its host-clock evidence."""

    gemini: FrameBundle
    d435i: FrameBundle
    request_start_skew_ms: float
    host_timestamp_skew_ms: float

    def audit_payload(self) -> dict[str, Any]:
        return {
            "request_start_skew_ms": self.request_start_skew_ms,
            "host_timestamp_skew_ms": self.host_timestamp_skew_ms,
            "gemini_host_timestamp_ns": self.gemini.host_timestamp_ns,
            "d435i_host_timestamp_ns": self.d435i.host_timestamp_ns,
            "gemini_device_timestamp": self.gemini.device_timestamp,
            "d435i_device_timestamp": self.d435i.device_timestamp,
            "gemini_frame_number": self.gemini.frame_number,
            "d435i_frame_number": self.d435i.frame_number,
        }


@dataclass(frozen=True)
class DualCaptureBurst:
    """A fixed-size group of host-near-synchronous frame pairs."""

    pairs: tuple[DualFramePair, ...]
    max_host_timestamp_skew_ms: float

    def audit_payload(self) -> dict[str, Any]:
        return {
            "synchronization_kind": "host_clock_near_sync",
            "pair_count": len(self.pairs),
            "max_host_timestamp_skew_ms": self.max_host_timestamp_skew_ms,
            "pairs": [pair.audit_payload() for pair in self.pairs],
        }


class DualCameraCaptureCoordinator:
    """Acquire a Gemini/D435i burst concurrently and measure pairing skew.

    It is intentionally independent of protocol storage.  A future dual-camera
    protocol must persist each camera's data under its own calibrated stream
    tree and attach :meth:`DualCaptureBurst.audit_payload` to its joint attempt.
    """

    def __init__(self, gemini: CameraAdapter, d435i: CameraAdapter) -> None:
        self.gemini = gemini
        self.d435i = d435i

    def assert_ready(self) -> None:
        expected = (("Gemini 336L", self.gemini, "C336L"), ("D435i", self.d435i, "CD435I"))
        for label, adapter, camera_code in expected:
            status: Mapping[str, Any] = adapter.get_status()
            device = status.get("device") or {}
            if not status.get("connected") or device.get("camera_code") != camera_code:
                raise DualCameraCaptureError(f"{label} 未连接或设备型号未通过协议识别")

    async def capture_burst(
        self,
        *,
        frame_count: int = 5,
        interval_ms: float = 150.0,
        max_host_timestamp_skew_ms: float = 75.0,
        timeout_ms: int = 1500,
    ) -> DualCaptureBurst:
        if frame_count < 1:
            raise ValueError("frame_count must be positive")
        if interval_ms < 0 or max_host_timestamp_skew_ms <= 0 or timeout_ms < 1:
            raise ValueError("invalid dual capture timing parameters")
        self.assert_ready()

        pairs: list[DualFramePair] = []
        for index in range(frame_count):
            request_started: dict[str, int] = {}

            def get_frame(camera_name: str, adapter: CameraAdapter):
                request_started[camera_name] = time.monotonic_ns()
                return adapter.get_frames(timeout_ms)

            gemini_task = asyncio.to_thread(get_frame, "gemini", self.gemini)
            d435i_task = asyncio.to_thread(get_frame, "d435i", self.d435i)
            gemini, d435i = await asyncio.gather(gemini_task, d435i_task)
            if gemini is None or d435i is None:
                missing = "Gemini 336L" if gemini is None else "D435i"
                raise DualCameraCaptureError(f"双机第 {index + 1} 对帧获取失败：{missing} 无数据")
            for camera_code, frame in (("C336L", gemini), ("CD435I", d435i)):
                try:
                    contract = validate_frame_contract(frame, camera_code)
                except FrameContractError as exc:
                    raise DualCameraCaptureError(
                        f"双机第 {index + 1} 对帧校验失败：{exc}"
                    ) from exc
                frame.camera_metadata["frame_contract"] = contract
            request_skew_ms = abs(
                request_started["gemini"] - request_started["d435i"]
            ) / 1_000_000.0
            host_skew_ms = abs(gemini.host_timestamp_ns - d435i.host_timestamp_ns) / 1_000_000.0
            if host_skew_ms > max_host_timestamp_skew_ms:
                raise DualCameraCaptureError(
                    f"双机第 {index + 1} 对帧 host 时间差 {host_skew_ms:.1f} ms 超过 "
                    f"{max_host_timestamp_skew_ms:.1f} ms"
                )
            pairs.append(DualFramePair(gemini, d435i, request_skew_ms, host_skew_ms))
            if index + 1 < frame_count:
                await asyncio.sleep(interval_ms / 1000.0)
        max_skew = max(pair.host_timestamp_skew_ms for pair in pairs)
        return DualCaptureBurst(tuple(pairs), max_skew)
