r"""Run a continuous, non-recording RGB-D camera stability check.

The tool never writes participant data or image frames.  It verifies that the
selected protocol camera keeps producing complete synchronized bundles and can
optionally persist the compact JSON report supplied with ``--output``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.camera_adapters import (  # noqa: E402
    FrameBundle,
    OrbbecCameraAdapter,
    RealSenseCameraAdapter,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="连续相机预览稳定性验收")
    parser.add_argument(
        "--backend",
        choices=("orbbec", "realsense"),
        required=True,
        help="Gemini 336L 选 orbbec；D435i 选 realsense",
    )
    parser.add_argument("--device-id", default="", help="可选的设备 ID/序列号")
    parser.add_argument(
        "--duration-seconds",
        type=int,
        default=600,
        help="连续取流秒数，正式稳定性验收使用 600",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="可选 JSON 报告路径；不会保存任何图像",
    )
    return parser.parse_args()


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _calibration_sha256(bundle: FrameBundle) -> str:
    payload = {
        "depth_scale": bundle.depth_scale,
        "intrinsics": _jsonable(bundle.intrinsics),
        "extrinsics": _jsonable(bundle.extrinsics),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _bundle_failures(bundle: FrameBundle) -> list[str]:
    failures: list[str] = []
    if bundle.color is None:
        failures.append("MISSING_COLOR")
    elif bundle.color.ndim != 3 or bundle.color.shape[2] != 3:
        failures.append("INVALID_COLOR_SHAPE")
    if bundle.depth_raw is None:
        failures.append("MISSING_DEPTH_RAW")
    elif bundle.depth_raw.ndim != 2:
        failures.append("INVALID_DEPTH_RAW_SHAPE")
    if bundle.depth_aligned is None:
        failures.append("MISSING_DEPTH_ALIGNED")
    elif bundle.depth_aligned.ndim != 2:
        failures.append("INVALID_DEPTH_ALIGNED_SHAPE")
    elif bundle.color is not None and bundle.depth_aligned.shape != bundle.color.shape[:2]:
        failures.append("ALIGNED_DEPTH_COLOR_SHAPE_MISMATCH")
    for side in ("left", "right"):
        infrared = bundle.infrared.get(side)
        if infrared is None:
            failures.append(f"MISSING_INFRARED_{side.upper()}")
        elif infrared.ndim != 2:
            failures.append(f"INVALID_INFRARED_{side.upper()}_SHAPE")
    if not bundle.intrinsics:
        failures.append("MISSING_INTRINSICS")
    if not bundle.extrinsics:
        failures.append("MISSING_EXTRINSICS")
    if bundle.depth_scale is None or float(bundle.depth_scale) <= 0:
        failures.append("INVALID_DEPTH_SCALE")
    if bundle.device_timestamp is None:
        failures.append("MISSING_DEVICE_TIMESTAMP")
    if bundle.frame_number is None:
        failures.append("MISSING_FRAME_NUMBER")
    return failures


def _write_report(path: Path | None, report: dict[str, Any]) -> None:
    if path is None:
        return
    target = path if path.is_absolute() else PROJECT_ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def run(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    if not 1 <= args.duration_seconds <= 3600:
        raise ValueError("--duration-seconds 必须在 1–3600 之间")
    adapter = (
        OrbbecCameraAdapter()
        if args.backend == "orbbec"
        else RealSenseCameraAdapter()
    )
    devices = adapter.list_devices()
    device_id = args.device_id or (devices[0]["id"] if devices else "")
    height = 800 if args.backend == "orbbec" else 720
    connected = adapter.connect(
        device_id=device_id,
        width=1280,
        height=height,
        fps=30,
    )
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "backend": args.backend,
        "device_id": device_id,
        "devices": devices,
        "requested_duration_seconds": args.duration_seconds,
        "connected": connected,
    }
    if not connected:
        report["hardware_pass"] = False
        report["error"] = adapter.get_status().get("message") or "相机连接失败"
        report["adapter_status"] = adapter.get_status()
        _write_report(args.output, report)
        return report, False

    started = time.monotonic()
    last_progress = started
    frame_count = 0
    timeout_count = 0
    failure_counts: Counter[str] = Counter()
    calibration_hashes: Counter[str] = Counter()
    previous_primary_number: int | None = None
    previous_device_timestamp: float | None = None
    primary_frame_gap_count = 0
    estimated_skipped_primary_frames = 0
    nonmonotonic_primary_frames = 0
    nonmonotonic_device_timestamps = 0
    previous_stream_frame_numbers: dict[str, int] = {}
    previous_stream_timestamps: dict[str, float] = {}
    stream_frame_gap_counts: Counter[str] = Counter()
    stream_estimated_skipped_frames: Counter[str] = Counter()
    stream_nonmonotonic_frame_numbers: Counter[str] = Counter()
    stream_nonmonotonic_timestamps: Counter[str] = Counter()
    clock_anomaly_samples: list[dict[str, Any]] = []
    interval_min_ms: float | None = None
    interval_max_ms: float | None = None
    interval_sum_ms = 0.0
    interval_count = 0
    interrupted = False

    try:
        while time.monotonic() - started < args.duration_seconds:
            bundle = adapter.get_frames(timeout_ms=2000)
            if bundle is None:
                timeout_count += 1
                continue
            frame_count += 1
            failure_counts.update(_bundle_failures(bundle))
            calibration_hashes[_calibration_sha256(bundle)] += 1

            if bundle.frame_number is not None:
                current_number = int(bundle.frame_number)
                if previous_primary_number is not None:
                    delta = current_number - previous_primary_number
                    if delta <= 0:
                        nonmonotonic_primary_frames += 1
                        if len(clock_anomaly_samples) < 20:
                            clock_anomaly_samples.append(
                                {
                                    "kind": "primary_frame_number_nonmonotonic",
                                    "bundle_index": frame_count,
                                    "previous": previous_primary_number,
                                    "current": current_number,
                                }
                            )
                    elif delta > 1:
                        primary_frame_gap_count += 1
                        estimated_skipped_primary_frames += delta - 1
                previous_primary_number = current_number

            if bundle.device_timestamp is not None:
                current_timestamp = float(bundle.device_timestamp)
                if previous_device_timestamp is not None:
                    interval = current_timestamp - previous_device_timestamp
                    if interval <= 0:
                        nonmonotonic_device_timestamps += 1
                        if len(clock_anomaly_samples) < 20:
                            clock_anomaly_samples.append(
                                {
                                    "kind": "primary_timestamp_nonmonotonic",
                                    "bundle_index": frame_count,
                                    "previous": previous_device_timestamp,
                                    "current": current_timestamp,
                                }
                            )
                    else:
                        interval_min_ms = (
                            interval
                            if interval_min_ms is None
                            else min(interval_min_ms, interval)
                        )
                        interval_max_ms = (
                            interval
                            if interval_max_ms is None
                            else max(interval_max_ms, interval)
                        )
                        interval_sum_ms += interval
                        interval_count += 1
                previous_device_timestamp = current_timestamp

            for name, value in bundle.stream_frame_numbers.items():
                current_number = int(value)
                previous_number = previous_stream_frame_numbers.get(name)
                if previous_number is not None:
                    delta = current_number - previous_number
                    if delta <= 0:
                        stream_nonmonotonic_frame_numbers[name] += 1
                        if len(clock_anomaly_samples) < 20:
                            clock_anomaly_samples.append(
                                {
                                    "kind": "stream_frame_number_nonmonotonic",
                                    "stream": name,
                                    "bundle_index": frame_count,
                                    "previous": previous_number,
                                    "current": current_number,
                                }
                            )
                    elif delta > 1:
                        stream_frame_gap_counts[name] += 1
                        stream_estimated_skipped_frames[name] += delta - 1
                previous_stream_frame_numbers[name] = current_number

            for name, value in bundle.stream_timestamps.items():
                current_timestamp = float(value)
                previous_timestamp = previous_stream_timestamps.get(name)
                if (
                    previous_timestamp is not None
                    and current_timestamp <= previous_timestamp
                ):
                    stream_nonmonotonic_timestamps[name] += 1
                    if len(clock_anomaly_samples) < 20:
                        clock_anomaly_samples.append(
                            {
                                "kind": "stream_timestamp_nonmonotonic",
                                "stream": name,
                                "bundle_index": frame_count,
                                "previous": previous_timestamp,
                                "current": current_timestamp,
                            }
                        )
                previous_stream_timestamps[name] = current_timestamp

            now = time.monotonic()
            if now - last_progress >= 30:
                print(
                    f"progress elapsed={now - started:.1f}s frames={frame_count} "
                    f"timeouts={timeout_count} failures={sum(failure_counts.values())}",
                    file=sys.stderr,
                    flush=True,
                )
                last_progress = now
    except KeyboardInterrupt:
        interrupted = True
    finally:
        adapter.disconnect()

    elapsed = time.monotonic() - started
    report.update(
        {
            "completed_duration_seconds": round(elapsed, 3),
            "frame_count": frame_count,
            "observed_fps": round(frame_count / elapsed, 3) if elapsed else 0.0,
            "timeout_count": timeout_count,
            "failure_counts": dict(sorted(failure_counts.items())),
            "calibration_sha256_counts": dict(calibration_hashes),
            "primary_frame_gap_count": primary_frame_gap_count,
            "estimated_skipped_primary_frames": estimated_skipped_primary_frames,
            "nonmonotonic_primary_frames": nonmonotonic_primary_frames,
            "nonmonotonic_device_timestamps": nonmonotonic_device_timestamps,
            "stream_frame_gap_counts": dict(sorted(stream_frame_gap_counts.items())),
            "stream_estimated_skipped_frames": dict(
                sorted(stream_estimated_skipped_frames.items())
            ),
            "stream_nonmonotonic_frame_numbers": dict(
                sorted(stream_nonmonotonic_frame_numbers.items())
            ),
            "stream_nonmonotonic_timestamps": dict(
                sorted(stream_nonmonotonic_timestamps.items())
            ),
            "clock_anomaly_samples": clock_anomaly_samples,
            "device_interval_ms": {
                "min": interval_min_ms,
                "mean": interval_sum_ms / interval_count if interval_count else None,
                "max": interval_max_ms,
                "count": interval_count,
            },
            "interrupted": interrupted,
        }
    )
    minimum_observed_fps = 10.0
    report["minimum_observed_fps"] = minimum_observed_fps
    report["warning_codes"] = (
        ["HOST_SAMPLING_SKIPPED_DEVICE_FRAMES"]
        if primary_frame_gap_count or stream_frame_gap_counts
        else []
    )
    passed = (
        not interrupted
        and elapsed >= args.duration_seconds
        and frame_count > 0
        and frame_count / elapsed >= minimum_observed_fps
        and timeout_count == 0
        and not failure_counts
        and len(calibration_hashes) == 1
        and nonmonotonic_primary_frames == 0
        and nonmonotonic_device_timestamps == 0
        and not stream_nonmonotonic_frame_numbers
        and not stream_nonmonotonic_timestamps
    )
    report["hardware_pass"] = passed
    _write_report(args.output, report)
    return report, passed


def main() -> int:
    args = parse_args()
    try:
        report, passed = run(args)
    except Exception as exc:
        report = {
            "schema_version": "1.0",
            "hardware_pass": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        _write_report(args.output, report)
        passed = False
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
