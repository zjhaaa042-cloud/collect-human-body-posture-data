r"""恢复并严格审计一个隔离的 DEV_ONLY 协议运行目录。

只允许处理项目内路径名含 ``dev_only`` 的数据根。可选地读取并校验待复核
WARN attempt 的 F03 RGB/对齐深度，然后以 REJECT 写入复核记录；不会接受测试
画面，也不会调用正式完成门禁。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.protocol_store import ProtocolStore  # noqa: E402
from backend.server.ws_server import WebSocketServer  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="恢复并审计隔离 DEV_ONLY 协议运行")
    parser.add_argument("--protocol-root", type=Path, required=True)
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--reject-pending-warn",
        action="store_true",
        help="校验 F03 后安全驳回所有待复核 WARN attempt",
    )
    parser.add_argument(
        "--acknowledge-dev-only",
        action="store_true",
        help="确认目标仅为隔离联调数据，不属于正式数据集",
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
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


async def _discard_message(_message: dict) -> None:
    return None


def _audit_errors(report: Mapping[str, Any]) -> list[str]:
    return [
        str(error)
        for subject_report in report.get("subjects", [])
        for error in subject_report.get("audit_errors", [])
    ]


async def recover_run(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    if not args.acknowledge_dev_only:
        raise ValueError("必须显式传入 --acknowledge-dev-only")
    protocol_root = _resolve_project_path(args.protocol_root, label="--protocol-root")
    if "dev_only" not in protocol_root.as_posix().lower():
        raise ValueError("--protocol-root 必须包含 dev_only，拒绝操作正式数据根")
    if not protocol_root.is_dir():
        raise ValueError(f"协议根目录不存在：{protocol_root}")

    report: dict[str, Any] = {
        "schema_version": "1.0",
        "mode": "DEV_ONLY_RECOVERY",
        "formal_dataset_eligible": False,
        "protocol_root": protocol_root.relative_to(PROJECT_ROOT).as_posix(),
        "subject_id": args.subject_id,
        "reject_pending_warn": bool(args.reject_pending_warn),
    }
    store: ProtocolStore | None = None
    try:
        store = ProtocolStore(protocol_root, dataset_phase="capture")
        report["startup_recovery"] = store.startup_recovery_report

        if args.reject_pending_warn:
            server = WebSocketServer.__new__(WebSocketServer)
            server.protocol_store = store
            server.active_protocol_subject_id = args.subject_id
            server._protocol_reconciliation_required_subjects = set()
            server._broadcast = _discard_message
            raw = store.get_subject_state(args.subject_id)
            reviewed = []
            for condition_id, condition_state in raw.get("conditions", {}).items():
                if condition_state.get("status") != "REVIEW_REQUIRED":
                    continue
                attempt_id = next(
                    (
                        attempt_id
                        for attempt_id in reversed(
                            list(condition_state.get("attempt_ids", []))
                        )
                        if raw.get("attempts", {}).get(attempt_id, {}).get(
                            "quality_status"
                        )
                        == "WARN"
                        and raw.get("attempts", {}).get(attempt_id, {}).get(
                            "review_status"
                        )
                        in {None, "PENDING"}
                    ),
                    None,
                )
                if not attempt_id:
                    continue
                preview = await asyncio.to_thread(
                    server._load_protocol_review_preview,
                    args.subject_id,
                    condition_id,
                    attempt_id,
                )
                result = await server._review_protocol_capture(
                    None,
                    {
                        "subject_id": args.subject_id,
                        "condition_id": condition_id,
                        "attempt_id": attempt_id,
                        "decision": "REJECT",
                        "reason": "DEV_ONLY 中断恢复演练：F03 证据已校验，测试画面不得进入正式数据集",
                        "evidence_token": preview["evidence_token"],
                    },
                )
                if result.get("reconciliation_required"):
                    store.recover(
                        args.subject_id,
                        verify_committed=True,
                        strict=True,
                    )
                    server._protocol_reconciliation_required_subjects.discard(
                        args.subject_id
                    )
                reviewed.append(
                    {
                        "condition_id": condition_id,
                        "attempt_id": attempt_id,
                        "decision": "REJECT",
                        "evidence_sha256": preview.get("evidence_sha256"),
                        "evidence_source": preview.get("source"),
                        "bookkeeping_status": result.get("bookkeeping_status"),
                    }
                )
            report["reviewed_pending_warn"] = reviewed

        audit = store.recover(
            args.subject_id,
            verify_committed=True,
            strict=True,
        )
        completion = store.completion_report(args.subject_id)
        state = store.get_subject_state(args.subject_id)
        audit_errors = _audit_errors(audit)
        report["audit"] = {
            "verify_committed": audit.get("verify_committed"),
            "subjects_scanned": audit.get("subjects_scanned"),
            "recovered_commits": audit.get("recovered_commits"),
            "write_failed_attempts": audit.get("write_failed_attempts"),
            "aborted_attempts": audit.get("aborted_attempts"),
            "errors": audit.get("errors", []),
            "audit_errors": audit_errors,
        }
        report["state"] = {
            "status": state.get("status"),
            "condition_statuses": {
                condition_id: condition_state.get("status")
                for condition_id, condition_state in state.get("conditions", {}).items()
            },
            "attempt_statuses": {
                attempt_id: {
                    "status": attempt.get("status"),
                    "quality_status": attempt.get("quality_status"),
                    "review_status": attempt.get("review_status"),
                    "file_count": len(attempt.get("files", [])),
                }
                for attempt_id, attempt in state.get("attempts", {}).items()
            },
        }
        report["completion"] = {
            "status": completion.get("status"),
            "ready_to_complete": completion.get("ready_to_complete"),
            "integrity_errors": completion.get("integrity_errors", []),
            "formal_completion_intentionally_blocked": not bool(
                completion.get("ready_to_complete")
            ),
        }
        passed = bool(
            not audit.get("errors")
            and not audit_errors
            and not completion.get("integrity_errors")
            and all(
                attempt.get("status") == "COMMITTED"
                for attempt in state.get("attempts", {}).values()
            )
            and all(
                attempt.get("review_status") != "PENDING"
                for attempt in state.get("attempts", {}).values()
                if attempt.get("quality_status") == "WARN"
            )
        )
        report["recovery_pass"] = passed
        return report, passed
    except Exception as exc:
        report["recovery_pass"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report, False
    finally:
        if store is not None:
            store.close()


def main() -> int:
    args = parse_args()
    try:
        report, passed = asyncio.run(recover_run(args))
    except Exception as exc:
        report = {
            "schema_version": "1.0",
            "mode": "DEV_ONLY_RECOVERY",
            "formal_dataset_eligible": False,
            "subject_id": args.subject_id,
            "recovery_pass": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        passed = False

    report_path = args.report
    if report_path is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path = Path(
            f"reports/hardware/dev_only_recovery_{args.subject_id}_{timestamp}.json"
        )
    resolved_report = _resolve_project_path(report_path, label="--report")
    _atomic_write_json(resolved_report, report)
    terminal = {
        "mode": report.get("mode"),
        "subject_id": report.get("subject_id"),
        "reviewed_pending_warn": len(report.get("reviewed_pending_warn", [])),
        "condition_statuses": (report.get("state") or {}).get(
            "condition_statuses"
        ),
        "attempt_statuses": (report.get("state") or {}).get("attempt_statuses"),
        "audit_errors": (report.get("audit") or {}).get("audit_errors"),
        "recovery_pass": report.get("recovery_pass"),
        "error": report.get("error"),
        "report": resolved_report.relative_to(PROJECT_ROOT).as_posix(),
    }
    print(json.dumps(terminal, ensure_ascii=False, indent=2))
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
