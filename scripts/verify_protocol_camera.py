r"""RealAnthro 协议相机五帧 burst 真机验收工具。

示例：
    .venv\Scripts\python.exe scripts\verify_protocol_camera.py --backend orbbec
    .venv\Scripts\python.exe scripts\verify_protocol_camera.py --backend realsense --bursts 100 \
        --output reports\hardware\d435i_bursts_100.json --summary-only

WARN（尤其 HUMAN_CONTENT_MANUAL_REVIEW）不会令硬件验收失败；任何 QC FAIL
都会令进程以非零状态退出。脚本不写入受试者数据目录。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace


for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.camera_adapters import (  # noqa: E402
    OrbbecCameraAdapter,
    RealSenseCameraAdapter,
)
from backend.protocol import Condition  # noqa: E402
from backend.server.ws_server import WebSocketServer  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="验证协议相机的同步五帧 burst")
    parser.add_argument(
        "--backend",
        choices=("orbbec", "realsense"),
        required=True,
        help="Gemini 336L 选 orbbec；D435i 选 realsense",
    )
    parser.add_argument("--device-id", default="", help="可选的设备 ID/序列号")
    parser.add_argument("--bursts", type=int, default=1, help="连续 burst 次数")
    parser.add_argument("--distance-mm", type=int, default=0, help="标称站位距离")
    parser.add_argument(
        "--output",
        type=Path,
        help="可选 JSON 报告路径；相对路径按项目根目录解析并原子写入",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="终端仅输出摘要，完整 burst 明细仍写入 --output",
    )
    return parser.parse_args()


def _write_report(path: Path | None, report: dict) -> None:
    if path is None:
        return
    target = path if path.is_absolute() else PROJECT_ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.tmp")
    temp.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(target)


def _terminal_report(report: dict, *, summary_only: bool) -> dict:
    if not summary_only:
        return report
    keys = (
        "schema_version",
        "backend",
        "camera_code",
        "device_id",
        "connected",
        "requested_bursts",
        "completed_bursts",
        "hard_passed_bursts",
        "hardware_pass",
        "failed_burst_index",
        "error",
    )
    return {key: report[key] for key in keys if key in report}


async def verify(args: argparse.Namespace) -> tuple[dict, bool]:
    if not 1 <= args.bursts <= 1000:
        raise ValueError("--bursts 必须在 1–1000 之间")
    adapter = (
        OrbbecCameraAdapter()
        if args.backend == "orbbec"
        else RealSenseCameraAdapter()
    )
    camera_code = "C336L" if args.backend == "orbbec" else "CD435I"
    distance_mm = args.distance_mm or (2500 if args.backend == "orbbec" else 3000)
    devices = adapter.list_devices()
    device_id = args.device_id or (devices[0]["id"] if devices else "")
    height = 800 if args.backend == "orbbec" else 720
    connected = adapter.connect(
        device_id=device_id,
        width=1280,
        height=height,
        fps=30,
    )
    report = {
        "schema_version": "1.0",
        "backend": args.backend,
        "camera_code": camera_code,
        "device_id": device_id,
        "devices": devices,
        "connected": connected,
        "requested_bursts": args.bursts,
        "completed_bursts": 0,
        "hard_passed_bursts": 0,
        "bursts": [],
    }
    if not connected:
        report["error"] = adapter.get_status().get("message") or "相机连接失败"
        report["adapter_status"] = adapter.get_status()
        return report, False

    server = WebSocketServer.__new__(WebSocketServer)
    server.active_camera_adapter = adapter
    server.camera = getattr(adapter, "manager", SimpleNamespace(enabled_ir_streams=[]))
    server.settings = SimpleNamespace(
        storage=SimpleNamespace(
            min_color_brightness=30,
            max_color_brightness=220,
            min_depth_coverage=0.30,
        )
    )
    condition = Condition(
        camera_code=camera_code,
        distance_mm=distance_mm,
        view_yaw_deg=0,
        suite="hardware_verification",
    )
    all_hard_pass = True
    try:
        for burst_index in range(1, args.bursts + 1):
            try:
                frames = await server._acquire_protocol_burst(adapter)
                qc = server._protocol_qc(frames, condition, adapter=adapter)
            except Exception as exc:
                all_hard_pass = False
                report["failed_burst_index"] = burst_index
                report["error"] = f"{type(exc).__name__}: {exc}"
                report["adapter_status"] = adapter.get_status()
                break
            hard_pass = not qc["failure_codes"]
            all_hard_pass = all_hard_pass and hard_pass
            report["completed_bursts"] += 1
            report["hard_passed_bursts"] += int(hard_pass)
            report["bursts"].append(
                {
                    "index": burst_index,
                    "status": qc["status"],
                    "hard_pass": hard_pass,
                    "failure_codes": qc["failure_codes"],
                    "warning_codes": qc["warning_codes"],
                    "frame_numbers": [frame.frame_number for frame in frames],
                    "device_intervals_ms": qc["burst_device_intervals_ms"],
                    "calibration_sha256": qc["calibration_sha256"],
                }
            )
            if burst_index % 10 == 0 or burst_index == args.bursts:
                print(
                    "progress "
                    f"bursts={burst_index}/{args.bursts} "
                    f"hard_passed={report['hard_passed_bursts']}",
                    file=sys.stderr,
                    flush=True,
                )
    finally:
        adapter.disconnect()
    report["hardware_pass"] = all_hard_pass and report["completed_bursts"] == args.bursts
    return report, bool(report["hardware_pass"])


def main() -> int:
    args = parse_args()
    try:
        report, passed = asyncio.run(verify(args))
    except Exception as exc:
        report = {
            "schema_version": "1.0",
            "hardware_pass": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        passed = False
    _write_report(args.output, report)
    print(
        json.dumps(
            _terminal_report(report, summary_only=args.summary_only),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
