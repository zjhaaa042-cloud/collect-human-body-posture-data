r"""执行一次隔离的 RealAnthro DEV_ONLY 真机写入链验收。

该工具会把一台相机的一个正式条件写入 ``data/dev_only_validation`` 下的
独立运行目录，验证五帧五模态落盘、sidecar、SHA-256、F03 已提交证据读取、
人工复核记录和全量恢复审计。为了避免把空场景/联调画面误标成合格人体数据，
工具固定以 ``REJECT`` 完成复核，最终条件应进入 ``NEEDS_RETAKE``。

示例：
    .venv\Scripts\python.exe scripts\verify_dev_only_capture.py \
        --backend realsense --device-id 243722074968 --acknowledge-dev-only

输出不进入正式 ``data/realanthro_rgbd_v1`` 数据根，且不会调用受试者完成门禁。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


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
from backend.core.protocol_store import ProtocolStore  # noqa: E402
from backend.protocol import format_condition_id, full31_no_lux  # noqa: E402
from backend.server.ws_server import WebSocketServer  # noqa: E402


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="隔离验证一次真实相机 DEV_ONLY 完整写入与 F03 复核链"
    )
    parser.add_argument(
        "--backend",
        choices=("orbbec", "realsense"),
        required=True,
        help="Gemini 336L 选 orbbec；D435i 选 realsense",
    )
    parser.add_argument("--device-id", default="", help="可选的设备 ID/序列号")
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=Path("data/dev_only_validation"),
        help="隔离图像与账本根目录，必须位于项目内且路径包含 dev_only",
    )
    parser.add_argument("--run-id", default="", help="可选的唯一运行 ID")
    parser.add_argument("--report", type=Path, help="可选的汇总 JSON 路径")
    parser.add_argument(
        "--operator-id",
        default="DEV_ONLY_OP",
        help="写入联调账本的操作员标识，不得使用真实受试者身份信息",
    )
    parser.add_argument(
        "--acknowledge-dev-only",
        action="store_true",
        help="确认本次仅为隔离联调、不会进入正式数据集",
    )
    return parser.parse_args()


def _resolve_project_path(path: Path, *, label: str) -> Path:
    target = (path if path.is_absolute() else PROJECT_ROOT / path).resolve()
    try:
        target.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} 必须位于项目目录内") from exc
    return target


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _relative_to_project(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()


async def _discard_message(_message: dict) -> None:
    return None


def _selected_condition(backend: str):
    camera_code = "C336L" if backend == "orbbec" else "CD435I"
    return next(
        condition
        for condition in full31_no_lux()
        if condition.camera_code == camera_code
    )


async def verify(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    if not args.acknowledge_dev_only:
        raise ValueError("必须显式传入 --acknowledge-dev-only")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", args.operator_id):
        raise ValueError("--operator-id 只能包含字母、数字、下划线或连字符，长度 1–32")

    run_id = args.run_id or datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError("--run-id 只能包含字母、数字、下划线或连字符")
    artifact_root = _resolve_project_path(args.artifact_root, label="--artifact-root")
    if "dev_only" not in artifact_root.as_posix().lower():
        raise ValueError("--artifact-root 路径必须包含 dev_only，避免误写正式数据根")
    run_root = artifact_root / f"{run_id}_{args.backend}"
    if run_root.exists():
        raise ValueError(f"运行目录已存在，禁止复用：{run_root}")

    protocol_root = (
        run_root
        / "realanthro_rgbd_v1"
        / "collections"
        / "dev_only_collection"
    )
    subject_id = "S9001" if args.backend == "orbbec" else "S9002"
    camera_code = "C336L" if args.backend == "orbbec" else "CD435I"
    adapter = (
        OrbbecCameraAdapter()
        if args.backend == "orbbec"
        else RealSenseCameraAdapter()
    )
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "mode": "DEV_ONLY",
        "formal_dataset_eligible": False,
        "run_id": run_id,
        "backend": args.backend,
        "camera_code": camera_code,
        "subject_id": subject_id,
        "artifact_root": _relative_to_project(run_root),
        "protocol_root": _relative_to_project(protocol_root),
        "review_policy": {
            "decision": "REJECT",
            "reason": "DEV_ONLY 联调画面仅用于验证 F03 证据链，不得进入正式人体数据集",
            "expected_condition_status": "NEEDS_RETAKE",
        },
    }
    store: ProtocolStore | None = None
    try:
        devices = adapter.list_devices()
        report["devices"] = devices
        device_id = args.device_id or (devices[0]["id"] if devices else "")
        report["device_id"] = device_id
        height = 800 if args.backend == "orbbec" else 720
        connected = adapter.connect(
            device_id=device_id,
            width=1280,
            height=height,
            fps=30,
        )
        report["connected"] = connected
        report["adapter_status"] = adapter.get_status()
        if not connected:
            raise RuntimeError(
                adapter.get_status().get("message") or f"{camera_code} 连接失败"
            )

        condition = _selected_condition(args.backend)
        condition_id = format_condition_id(condition)
        report["condition"] = {
            "condition_id": condition_id,
            "distance_mm": condition.distance_mm,
            "view_yaw_deg": condition.view_yaw_deg,
            "light_id": condition.light_id,
            "pose_id": condition.pose_id,
            "clothing_id": condition.clothing_id,
            "repeat_id": condition.repeat_id,
            "suite": condition.suite,
        }

        store = ProtocolStore(protocol_root, dataset_phase="capture")
        server = WebSocketServer.__new__(WebSocketServer)
        server.protocol_store = store
        server.settings = SimpleNamespace(
            storage=SimpleNamespace(
                min_color_brightness=30,
                max_color_brightness=220,
                min_depth_coverage=0.30,
            )
        )
        server.active_camera_adapter = adapter
        server.camera = getattr(
            adapter,
            "manager",
            SimpleNamespace(enabled_ir_streams=[]),
        )
        server.capture_lock = asyncio.Lock()
        server.camera_lock = asyncio.Lock()
        server.is_capturing = False
        server.is_shutting_down = False
        server.voice_synthesizer = None
        server.active_protocol_subject_id = subject_id
        server._broadcast = _discard_message

        conditions = (condition,)
        store.create_subject(
            subject_id=subject_id,
            protocol_version="RealAnthro-RGBD-v1.0",
            profile_id="full31_no_lux",
            subject_metadata={
                "operator_id": args.operator_id,
                "dev_only": True,
                "formal_dataset_eligible": False,
                "intended_scene": "non_participant_test_scene",
                "collection_scope": "isolated_write_chain_validation",
                "smplx_deferred": True,
            },
            expected_conditions=[server._condition_payload(condition)],
            capture_policy_version="realanthro-capture-v1.0",
            capture_policy=server._protocol_capture_policy(conditions),
        )
        state = server._protocol_subject_state(subject_id)
        nonce = state["conditions"][0]["confirmation_nonce"]

        print("progress stage=capture status=starting", file=sys.stderr, flush=True)
        capture = await server._capture_protocol_condition(
            None,
            {
                "subject_id": subject_id,
                "condition_id": condition_id,
                "confirmations": {
                    "distance_marker": True,
                    "pose_view_clothing": True,
                    "full_body_visible": True,
                    "nonce": nonce,
                    "dev_only": True,
                },
            },
        )
        report["capture"] = {
            "committed": bool(capture.get("committed")),
            "bookkeeping_status": capture.get("bookkeeping_status"),
            "reconciliation_required": bool(capture.get("reconciliation_required")),
            "attempt_id": capture.get("attempt_id"),
            "quality_status": capture.get("quality_status"),
            "failure_codes": (capture.get("qc") or {}).get("failure_codes", []),
            "warning_codes": (capture.get("qc") or {}).get("warning_codes", []),
            "calibration_sha256": (capture.get("qc") or {}).get(
                "calibration_sha256"
            ),
        }
        if not capture.get("committed"):
            raise RuntimeError("DEV_ONLY attempt 未完成原子提交")
        if capture.get("reconciliation_required"):
            print(
                "progress stage=capture_recovery status=reconciling",
                file=sys.stderr,
                flush=True,
            )
            report["capture_recovery"] = store.recover(
                subject_id,
                verify_committed=True,
                strict=True,
            )
            getattr(
                server,
                "_protocol_reconciliation_required_subjects",
                set(),
            ).discard(subject_id)
        if capture.get("quality_status") != "WARN":
            raise RuntimeError(
                "DEV_ONLY F03 复核链要求 WARN；本次为 "
                f"{capture.get('quality_status')}"
            )

        attempt_id = str(capture["attempt_id"])
        print("progress stage=f03_evidence status=verifying", file=sys.stderr, flush=True)
        preview = await asyncio.to_thread(
            server._load_protocol_review_preview,
            subject_id,
            condition_id,
            attempt_id,
        )
        report["f03_evidence"] = {
            "anchor_frame": preview.get("anchor_frame"),
            "source": preview.get("source"),
            "rgb_preview_present": bool(preview.get("color")),
            "depth_preview_present": bool(preview.get("depth")),
            "evidence_sha256": preview.get("evidence_sha256"),
            "verified_at": preview.get("evidence_verified_at"),
        }

        print("progress stage=review status=rejecting_dev_only", file=sys.stderr, flush=True)
        review_result = await server._review_protocol_capture(
            None,
            {
                "subject_id": subject_id,
                "condition_id": condition_id,
                "attempt_id": attempt_id,
                "decision": "REJECT",
                "reason": report["review_policy"]["reason"],
                "evidence_token": preview["evidence_token"],
            },
        )
        if review_result.get("reconciliation_required"):
            print(
                "progress stage=review_recovery status=reconciling",
                file=sys.stderr,
                flush=True,
            )
            report["review_recovery"] = store.recover(
                subject_id,
                verify_committed=True,
                strict=True,
            )
            getattr(
                server,
                "_protocol_reconciliation_required_subjects",
                set(),
            ).discard(subject_id)

        raw = store.get_subject_state(subject_id)
        attempt = raw["attempts"][attempt_id]
        condition_state = raw["conditions"][condition_id]
        files = list(attempt.get("files", []))
        modality_counts = Counter(str(item.get("modality")) for item in files)
        sidecars = dict(attempt.get("sidecars", {}))
        review = dict(attempt.get("review") or {})
        review_path = (
            protocol_root / "subjects" / subject_id / str(review.get("path", ""))
        ).resolve()
        expected_review_hash = str(review.get("file_sha256") or "")
        review_hash_verified = bool(
            expected_review_hash
            and review_path.is_file()
            and _sha256(review_path) == expected_review_hash
        )

        verified_anchor = store.get_verified_anchor_files(
            subject_id,
            condition_id,
            attempt_id,
            frame_index=3,
            modalities=("rgb", "depth_aligned"),
        )
        anchor_files = {
            modality: {
                key: value
                for key, value in record.items()
                if key != "bytes"
            }
            for modality, record in verified_anchor["files"].items()
        }

        print("progress stage=full_audit status=verifying", file=sys.stderr, flush=True)
        audit = store.recover(
            subject_id,
            verify_committed=True,
            strict=True,
        )
        completion = store.completion_report(subject_id)
        audit_errors = [
            error
            for subject_report in audit.get("subjects", [])
            for error in subject_report.get("audit_errors", [])
        ]
        report["write_chain"] = {
            "file_count": len(files),
            "expected_file_count": 35,
            "modality_counts": dict(sorted(modality_counts.items())),
            "sidecar_names": sorted(sidecars),
            "sidecars": sidecars,
            "anchor_files": anchor_files,
            "anchor_evidence_sha256": verified_anchor.get("evidence_sha256"),
            "review": {
                "decision": review.get("decision"),
                "review_status": review.get("review_status"),
                "path": review.get("path"),
                "file_sha256": expected_review_hash,
                "file_sha256_verified": review_hash_verified,
                "content_sha256": review.get("content_sha256"),
            },
            "condition_status_after_review": condition_state.get("status"),
            "accepted_attempt_id": condition_state.get("accepted_attempt_id"),
            "review_operation_committed": bool(review_result.get("committed")),
            "review_bookkeeping_status": review_result.get("bookkeeping_status"),
        }
        report["audit"] = {
            "verify_committed": audit.get("verify_committed"),
            "subjects_scanned": audit.get("subjects_scanned"),
            "write_failed_attempts": audit.get("write_failed_attempts"),
            "aborted_attempts": audit.get("aborted_attempts"),
            "audit_errors": audit_errors,
            "completion_status": completion.get("status"),
            "completion_integrity_errors": completion.get("integrity_errors", []),
            "formal_completion_intentionally_blocked": not bool(
                completion.get("ready_to_complete")
            ),
        }

        expected_modalities = {
            "rgb": 5,
            "depth_raw": 5,
            "depth_aligned": 5,
            "ir_left": 5,
            "ir_right": 5,
        }
        passed = all(
            (
                capture.get("committed") is True,
                capture.get("quality_status") == "WARN",
                len(files) == 35,
                dict(modality_counts) == expected_modalities,
                set(sidecars) == {"capture.json", "qc.json", "commit.json"},
                preview.get("source") == "verified_committed_files",
                bool(preview.get("color")),
                bool(preview.get("depth")),
                review.get("decision") == "REJECT",
                review.get("review_status") == "REJECTED",
                condition_state.get("status") == "NEEDS_RETAKE",
                condition_state.get("accepted_attempt_id") is None,
                review_hash_verified,
                not audit.get("errors"),
                not audit_errors,
                not completion.get("integrity_errors"),
            )
        )
        report["dev_only_pass"] = passed
        return report, passed
    except Exception as exc:
        report["dev_only_pass"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        try:
            report["adapter_status"] = adapter.get_status()
        except Exception:
            pass
        return report, False
    finally:
        try:
            adapter.disconnect()
        finally:
            if store is not None:
                store.close()


def main() -> int:
    args = parse_args()
    try:
        report, passed = asyncio.run(verify(args))
    except Exception as exc:
        report = {
            "schema_version": "1.0",
            "mode": "DEV_ONLY",
            "formal_dataset_eligible": False,
            "backend": args.backend,
            "dev_only_pass": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        passed = False

    run_root_text = report.get("artifact_root")
    if run_root_text:
        _atomic_write_json(
            _resolve_project_path(Path(str(run_root_text)), label="artifact report")
            / "dev_only_report.json",
            report,
        )
    report_path = args.report
    if report_path is None:
        run_id = str(report.get("run_id") or "failed")
        report_path = Path(
            f"reports/hardware/dev_only_{args.backend}_{run_id}.json"
        )
    resolved_report = _resolve_project_path(report_path, label="--report")
    _atomic_write_json(resolved_report, report)

    terminal = {
        "schema_version": report.get("schema_version"),
        "mode": report.get("mode"),
        "backend": report.get("backend"),
        "camera_code": report.get("camera_code"),
        "device_id": report.get("device_id"),
        "run_id": report.get("run_id"),
        "artifact_root": report.get("artifact_root"),
        "condition_id": (report.get("condition") or {}).get("condition_id"),
        "attempt_id": (report.get("capture") or {}).get("attempt_id"),
        "quality_status": (report.get("capture") or {}).get("quality_status"),
        "file_count": (report.get("write_chain") or {}).get("file_count"),
        "condition_status_after_review": (report.get("write_chain") or {}).get(
            "condition_status_after_review"
        ),
        "audit_errors": (report.get("audit") or {}).get("audit_errors"),
        "dev_only_pass": report.get("dev_only_pass"),
        "error": report.get("error"),
        "report": _relative_to_project(resolved_report),
    }
    print(json.dumps(terminal, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
