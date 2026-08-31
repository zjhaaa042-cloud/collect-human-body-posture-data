"""DEV_ONLY real-hardware smoke test for the dual PNG/NPY commit chain."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config.settings import get_settings, load_settings
from backend.core.camera_adapters import OrbbecCameraAdapter, RealSenseCameraAdapter
from backend.core.camera_manager import CameraManager
from backend.core.dual_capture import DualCameraCaptureCoordinator
from backend.core.dual_session_store import DualSessionStore
from backend.utils.frame_processor import FrameProcessor


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="连接 Gemini 336L 与 D435i，采集一个隔离的双机五帧组并校验 PNG/NPY/哈希。"
    )
    parser.add_argument("--gemini-device-id", default="")
    parser.add_argument("--d435i-device-id", default="")
    parser.add_argument(
        "--output-root",
        default=str(ROOT / "data" / "dev_only_validation" / "dual_npy_smoke"),
    )
    parser.add_argument(
        "--report",
        default=str(ROOT / "reports" / "hardware" / "dual_npy_smoke.json"),
    )
    parser.add_argument("--acknowledge-dev-only", action="store_true")
    return parser.parse_args()


def assert_dev_only(path: Path, acknowledged: bool) -> None:
    if not acknowledged:
        raise RuntimeError("必须显式传入 --acknowledge-dev-only")
    resolved = path.resolve()
    resolved.relative_to(ROOT.resolve())
    if "dev_only" not in str(resolved).lower():
        raise RuntimeError("输出目录名称必须包含 dev_only，禁止写入正式数据目录")


def verify_npy_pairs(attempt_dir: Path, files: list[dict]) -> dict:
    checked = 0
    for record in files:
        if record.get("modality") not in {"depth_raw_npy", "depth_aligned_npy"}:
            continue
        npy_path = attempt_dir / record["path"]
        array = np.load(npy_path, allow_pickle=False)
        if array.dtype != np.uint16 or array.ndim != 2:
            raise RuntimeError(f"NPY dtype/shape 无效：{record['path']}")
        png_record = next(
            item for item in files
            if item.get("camera_code") == record.get("camera_code")
            and item.get("frame") == record.get("frame")
            and item.get("modality") == record.get("logical_modality")
        )
        encoded = np.frombuffer((attempt_dir / png_record["path"]).read_bytes(), dtype=np.uint8)
        png = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
        if png is None or not np.array_equal(png, array):
            raise RuntimeError(f"NPY 与 PNG 不一致：{record['path']}")
        checked += 1
    if checked != 20:
        raise RuntimeError(f"双机 NPY 数量错误：{checked}/20")
    return {"npy_files_checked": checked, "png_npy_equal": True}


def verify_rgb_and_alignment(attempt_dir: Path, files: list[dict], burst) -> dict:
    frames_by_camera = {
        "C336L": [pair.gemini for pair in burst.pairs],
        "CD435I": [pair.d435i for pair in burst.pairs],
    }
    preview_mae = []
    swapped_mae = []
    saved_checked = 0
    contract_checked = 0
    maximum_stream_skew_ms = 0.0
    for camera_code, frames in frames_by_camera.items():
        for index, frame in enumerate(frames, 1):
            rgb_record = next(
                item for item in files
                if item.get("camera_code") == camera_code
                and item.get("frame") == f"frame_{index:02d}"
                and item.get("modality") == "rgb"
            )
            saved_bgr = cv2.imdecode(
                np.frombuffer((attempt_dir / rgb_record["path"]).read_bytes(), dtype=np.uint8),
                cv2.IMREAD_COLOR,
            )
            saved_rgb = cv2.cvtColor(saved_bgr, cv2.COLOR_BGR2RGB)
            if not np.array_equal(saved_rgb, frame.color):
                raise RuntimeError(f"RGB PNG 色序/像素回读不一致：{rgb_record['path']}")
            saved_checked += 1

            height, width = frame.color.shape[:2]
            preview = FrameProcessor(
                preview_size=(width, height), jpeg_quality=100
            ).encode_preview(frame.color, is_rgb=True)
            preview_bgr = cv2.imdecode(
                np.frombuffer(base64.b64decode(preview), dtype=np.uint8),
                cv2.IMREAD_COLOR,
            )
            preview_rgb = cv2.cvtColor(preview_bgr, cv2.COLOR_BGR2RGB)
            correct = float(
                np.mean(np.abs(preview_rgb.astype(np.int16) - frame.color.astype(np.int16)))
            )
            swapped = float(
                np.mean(
                    np.abs(
                        preview_rgb.astype(np.int16)
                        - frame.color[..., ::-1].astype(np.int16)
                    )
                )
            )
            if correct > 8.0 or correct >= swapped:
                raise RuntimeError(
                    f"RGB 预览色序校验失败：{camera_code} frame_{index:02d}, "
                    f"mae={correct:.3f}, swapped_mae={swapped:.3f}"
                )
            preview_mae.append(correct)
            swapped_mae.append(swapped)

            contract = frame.camera_metadata.get("frame_contract") or {}
            if contract.get("schema_version") != "rgbd-frame-contract-v1":
                raise RuntimeError(f"缺少 RGB-D 对齐契约：{camera_code} frame_{index:02d}")
            if not contract["spatial_alignment"]["depth_aligned_matches_rgb_pixels"]:
                raise RuntimeError(f"aligned depth 未对齐 RGB：{camera_code} frame_{index:02d}")
            maximum_stream_skew_ms = max(
                maximum_stream_skew_ms,
                float(contract["temporal_alignment"]["stream_timestamp_skew_ms"]),
            )
            contract_checked += 1
    return {
        "rgb_png_files_checked": saved_checked,
        "rgb_png_exact_roundtrip": True,
        "rgb_preview_frames_checked": len(preview_mae),
        "rgb_preview_mean_absolute_error_max": max(preview_mae),
        "rgb_preview_swapped_channel_error_min": min(swapped_mae),
        "rgb_preview_channel_order": "RGB",
        "rgbd_frame_contracts_checked": contract_checked,
        "aligned_depth_matches_rgb_pixels": True,
        "raw_depth_coordinate_system": "native_depth_camera",
        "max_intra_camera_stream_timestamp_skew_ms": maximum_stream_skew_ms,
    }


async def run(args: argparse.Namespace) -> dict:
    output_root = Path(args.output_root)
    assert_dev_only(output_root, args.acknowledge_dev_only)
    config_path = ROOT / "config.json"
    settings = load_settings(str(config_path)) if config_path.exists() else get_settings()
    gemini = OrbbecCameraAdapter(CameraManager(settings.camera.orientation))
    d435i = RealSenseCameraAdapter()
    store = None
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        gemini_ok = await asyncio.to_thread(
            gemini.connect,
            device_id=args.gemini_device_id,
            width=settings.camera.width,
            height=settings.camera.height,
            fps=settings.camera.fps,
            params_file=settings.camera.params_file,
            enable_infrared=False,
        )
        if not gemini_ok:
            raise RuntimeError(gemini.get_status().get("message") or "Gemini 336L 连接失败")
        d435i_ok = await asyncio.to_thread(
            d435i.connect,
            device_id=args.d435i_device_id,
            width=settings.camera.width,
            height=720,
            fps=settings.camera.fps,
            enable_infrared=False,
        )
        if not d435i_ok:
            raise RuntimeError(d435i.get_status().get("message") or "D435i 连接失败")
        burst = await DualCameraCaptureCoordinator(gemini, d435i).capture_burst(
            frame_count=5, interval_ms=150.0, max_host_timestamp_skew_ms=75.0
        )
        store = DualSessionStore(output_root)
        subject_id = f"DEVONLY_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
        store.create_session(subject_id, clothing_note="DEV_ONLY empty-scene smoke")
        committed = await asyncio.to_thread(
            store.commit_group,
            subject_id,
            0,
            [pair.gemini for pair in burst.pairs],
            [pair.d435i for pair in burst.pairs],
            audit=burst.audit_payload(),
            metadata={"dev_only": True, "contains_authorized_subject": False},
        )
        attempt_dir = (
            store.root / "subjects" / subject_id / "angles" / "angle_000_front"
            / committed["attempt_id"]
        )
        files = committed["capture"]["files"]
        if len(files) != 80:
            raise RuntimeError(f"双机文件数量错误：{len(files)}/80")
        npy_report = verify_npy_pairs(attempt_dir, files)
        rgb_alignment_report = verify_rgb_and_alignment(attempt_dir, files, burst)
        report = {
            "success": True,
            "dev_only": True,
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "output_root": str(output_root.resolve()),
            "subject_id": subject_id,
            "attempt_id": committed["attempt_id"],
            "file_count": len(files),
            "max_host_timestamp_skew_ms": burst.max_host_timestamp_skew_ms,
            "storage_features": committed["capture"].get("storage_features"),
            "commit_json": str(attempt_dir / "commit.json"),
            **npy_report,
            **rgb_alignment_report,
        }
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        if store is not None:
            store.close()
        await asyncio.to_thread(gemini.disconnect)
        await asyncio.to_thread(d435i.disconnect)


def main() -> int:
    args = parse_args()
    try:
        report = asyncio.run(run(args))
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
