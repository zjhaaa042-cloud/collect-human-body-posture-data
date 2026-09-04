"""Append-only storage for protocol-driven RGB-D data collection.

This module deliberately has no dependency on a camera SDK.  It accepts numpy
arrays carried by dictionaries, dataclasses, pydantic models, or ordinary
objects and turns a five-frame synchronized burst into an auditable capture
attempt.

The legacy :mod:`data_collector` remains untouched.  ``ProtocolStore`` is the
storage boundary for the protocol workflow and can therefore be introduced
incrementally by the websocket layer.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

import cv2
import numpy as np

from ..storage.atomic_io import atomic_write_npy


FRAME_COUNT = 5
CAPTURE_POLICY_VERSION = "capture-policy-v1.0"
PROTOCOL_SNAPSHOT_SCHEMA_VERSION = "1.0"
COMMIT_RECORD_FILENAME = "commit.json"
_WINDOWS_ATOMIC_REPLACE_RETRY_DELAYS_SEC = (0.1, 0.25, 0.5, 1.0, 2.0)
_WINDOWS_TRANSIENT_REPLACE_LOCK_ERRORS = {5, 32, 33}
ALL_MEASUREMENT_IDS: Tuple[str, ...] = tuple(f"M{i:02d}" for i in range(1, 24))
CORE_MEASUREMENT_IDS: Tuple[str, ...] = ("M01", "M03", "M06", "M09", "M12")
OPTIONAL_MEASUREMENT_IDS: Tuple[str, ...] = tuple(
    item for item in ALL_MEASUREMENT_IDS if item not in CORE_MEASUREMENT_IDS
)
LEGACY_CORE_MEASUREMENT_IDS: Tuple[str, ...] = tuple(
    f"M{i:02d}" for i in range(1, 14)
)
REQUIRED_ANTHROPOMETRY_EQUIPMENT_FIELDS: Tuple[str, ...] = (
    "stadiometer_id",
    "scale_id",
    "tape_id",
    "anthropometer_id",
)
ANTHROPOMETRY_HARD_RANGES: Dict[str, Tuple[float, float]] = {
    "height": (80.0, 250.0),
    "weight": (20.0, 400.0),
    "breadth": (10.0, 100.0),
    "length_or_circumference": (10.0, 300.0),
}
STRICT_QC_REQUIRED_CHECK_COUNTS: Dict[str, int] = {
    "BURST_FRAME_COUNT": 1,
    "REQUIRED_MODALITIES": FRAME_COUNT,
    "IMAGE_FORMAT_AND_SHAPE": FRAME_COUNT,
    "CALIBRATION_COMPLETE": FRAME_COUNT,
    "DEPTH_RAW_VALID_RATIO": FRAME_COUNT,
    "DEPTH_ALIGNED_VALID_RATIO": FRAME_COUNT,
    "STREAM_TIMESTAMPS_AND_SKEW": FRAME_COUNT,
    "STREAM_FRAME_NUMBERS_PRESENT": FRAME_COUNT,
    "CALIBRATION_STABLE_ACROSS_BURST": 1,
    "FRAME_NUMBERS_STRICTLY_INCREASING": 1,
    "BURST_DEVICE_INTERVAL_HARD": 1,
    "HUMAN_CONTENT_MANUAL_REVIEW": 1,
}

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CONDITION_ID_RE = re.compile(
    r"^(?P<camera>C[A-Z0-9]+)_D(?P<distance>\d{4,5})_V(?P<view>\d{3})_"
    r"(?P<light>L[A-Z0-9]+)_(?P<pose>P[A-Z0-9]+)_(?P<clothing>C[A-Z0-9]+)_"
    r"R(?P<repeat>\d{2,3})$"
)


class ProtocolStoreError(RuntimeError):
    """Base exception for protocol storage failures."""


class ProtocolValidationError(ProtocolStoreError, ValueError):
    """Input does not satisfy the frozen protocol contract."""


class SubjectExistsError(ProtocolStoreError):
    """A subject directory already exists and must not be overwritten."""


class SubjectNotFoundError(ProtocolStoreError):
    """The requested subject has not been created."""


class SubjectCompletedError(ProtocolStoreError):
    """An append was attempted after subject closure."""


class IncompleteSubjectError(ProtocolStoreError):
    """The subject cannot be closed because required data are missing."""

    def __init__(self, report: Mapping[str, Any]):
        self.report = dict(report)
        missing = ", ".join(self.report.get("missing_condition_ids", [])) or "none"
        super().__init__(
            "subject is incomplete: "
            f"missing conditions={missing}, "
            f"anthropometry_complete={self.report.get('anthropometry_complete', False)}"
        )


# Canonical field aliases and current project repeat thresholds.  Frozen subject
# snapshots retain the values in effect when that subject was created.
# M02 has no third-measurement trigger in the protocol.
try:
    # The shared dictionary is the single source of truth for field names,
    # including bilateral optional measurements (M16--M19 and M21).
    from backend.protocol.measurements import MEASUREMENTS_BY_ID as _SHARED_MEASUREMENTS

    _MEASUREMENT_DEFINITIONS: Dict[str, Dict[str, Any]] = {
        measurement_id: {
            "fields": tuple(definition.field_names),
            "unit": definition.unit,
            "kind": definition.kind,
            "threshold": definition.third_measurement_threshold,
            "required": definition.required,
        }
        for measurement_id, definition in _SHARED_MEASUREMENTS.items()
    }
except ImportError:  # pragma: no cover - standalone fallback for staged rollout
    _MEASUREMENT_DEFINITIONS = {
        "M01": {"fields": ("height_cm",), "unit": "cm", "kind": "height", "threshold": 1.0, "required": True},
        "M02": {"fields": ("weight_kg",), "unit": "kg", "kind": "weight", "threshold": None, "required": False},
        "M03": {"fields": ("biacromial_breadth_cm",), "unit": "cm", "threshold": 1.0, "required": True},
        "M04": {"fields": ("shoulder_girth_cm",), "unit": "cm", "threshold": 1.0, "required": False},
        "M05": {"fields": ("upper_chest_circumference_cm",), "unit": "cm", "threshold": 1.0, "required": False},
        "M06": {"fields": ("nipple_chest_circumference_cm",), "unit": "cm", "threshold": 2.0, "required": True},
        "M07": {"fields": ("underbust_circumference_cm",), "unit": "cm", "threshold": 1.0, "required": False},
        "M08": {"fields": ("natural_waist_circumference_cm",), "unit": "cm", "threshold": 1.0, "required": False},
        "M09": {"fields": ("midpoint_waist_circumference_cm",), "unit": "cm", "threshold": 1.5, "required": True},
        "M10": {"fields": ("umbilical_circumference_cm",), "unit": "cm", "threshold": 1.0, "required": False},
        "M11": {"fields": ("max_abdomen_circumference_cm",), "unit": "cm", "threshold": 1.0, "required": False},
        "M12": {"fields": ("max_hip_circumference_cm",), "unit": "cm", "threshold": 1.5, "required": True},
        "M13": {"fields": ("trochanter_pelvis_circumference_cm",), "unit": "cm", "threshold": 1.0, "required": False},
        "M14": {"fields": ("high_hip_circumference_cm",), "unit": "cm", "threshold": 1.0, "required": False},
        "M15": {"fields": ("neck_circumference_cm",), "unit": "cm", "threshold": 1.0, "required": False},
        "M16": {"fields": ("left_upper_arm_circumference_cm", "right_upper_arm_circumference_cm"), "unit": "cm", "threshold": 1.0, "required": False},
        "M17": {"fields": ("left_forearm_circumference_cm", "right_forearm_circumference_cm"), "unit": "cm", "threshold": 1.0, "required": False},
        "M18": {"fields": ("left_max_thigh_circumference_cm", "right_max_thigh_circumference_cm"), "unit": "cm", "threshold": 1.0, "required": False},
        "M19": {"fields": ("left_max_calf_circumference_cm", "right_max_calf_circumference_cm"), "unit": "cm", "threshold": 1.0, "required": False},
        "M20": {"fields": ("arm_span_cm",), "unit": "cm", "threshold": 0.5, "required": False},
        "M21": {"fields": ("left_arm_length_cm", "right_arm_length_cm"), "unit": "cm", "threshold": 0.5, "required": False},
        "M22": {"fields": ("inseam_cm",), "unit": "cm", "threshold": 0.5, "required": False},
        "M23": {"fields": ("acromion_to_midpoint_waist_cm",), "unit": "cm", "threshold": 0.5, "required": False},
    }

_FIELD_TO_MEASUREMENT_ID = {
    field_name.lower(): measurement_id
    for measurement_id, definition in _MEASUREMENT_DEFINITIONS.items()
    for field_name in definition["fields"]
}


class ProtocolStore:
    """Append-only, auditable subject storage.

    Parameters
    ----------
    base_dir:
        Root of a RealAnthro-RGBD dataset.
    dataset_phase:
        Usually ``"pilot"`` or ``"official"``.  It is a safe directory name,
        not a policy switch.
    protocol:
        Optional protocol registry/profile provider.  It may be a mapping, a
        callable, or expose ``get_profile(profile_id)``.  ``create_subject``
        can also receive ``expected_conditions`` directly.
    """

    def __init__(
        self,
        base_dir: os.PathLike[str] | str,
        dataset_phase: str = "capture",
        protocol: Any = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.dataset_phase = self._validate_safe_id(dataset_phase, "dataset_phase")
        self.protocol = protocol
        # The physical layout is intentionally independent of pilot/official
        # policy.  A later governance decision must not move or fork subject
        # data; dataset_phase remains auditable metadata only.
        self.subjects_dir = self.base_dir / "subjects"
        self.phase_dir = self.subjects_dir  # compatibility alias for callers
        self.manifests_dir = self.base_dir / "manifests"
        self.equipment_checks_dir = self.base_dir / "equipment_checks"
        self.subjects_dir.mkdir(parents=True, exist_ok=True)
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        self.equipment_checks_dir.mkdir(parents=True, exist_ok=True)
        self._locks_guard = threading.Lock()
        self._subject_locks: Dict[str, threading.RLock] = {}
        self._lease_handle = None
        self._lease_backend: Optional[str] = None
        self._acquire_dataset_lease()
        # Fast startup recovery verifies and reconciles incomplete work.  A
        # caller can request a full hash audit later with recover(
        # verify_committed=True).
        self.startup_recovery_report = self.recover(
            verify_committed=False,
            strict=False,
        )

    def _acquire_dataset_lease(self) -> None:
        """Hold a real OS-level exclusive lock for this dataset instance."""

        lock_path = self.base_dir / ".protocol_store.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                self._lease_backend = "msvcrt"
            else:  # pragma: no cover - exercised on POSIX CI
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._lease_backend = "fcntl"
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise ProtocolStoreError(
                "已有采集实例持有该数据集锁，拒绝启动恢复或并发写入"
            ) from exc
        self._lease_handle = handle

    def close(self) -> None:
        """Release the dataset lease; safe to call more than once."""

        handle = getattr(self, "_lease_handle", None)
        if handle is None:
            return
        try:
            handle.seek(0)
            if self._lease_backend == "msvcrt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            elif self._lease_backend == "fcntl":  # pragma: no cover - POSIX
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._lease_handle = None
            self._lease_backend = None

    def __enter__(self) -> "ProtocolStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - interpreter timing varies
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public subject API
    # ------------------------------------------------------------------
    def save_equipment_check(
        self,
        operator_id: str,
        equipment: Mapping[str, Any],
        *,
        note: str = "",
        check_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append an auditable daily anthropometry equipment check.

        A check is intentionally stored outside an individual subject so the
        same verified instruments can be referenced by multiple subjects on
        the same day.  Existing records are never overwritten: a re-check
        produces a new immutable record and becomes the latest one.
        """

        operator_id = self._validate_safe_id(operator_id, "operator_id")
        if not isinstance(equipment, Mapping):
            raise ProtocolValidationError("equipment check requires equipment mapping")
        try:
            recorded_date = date.fromisoformat(check_date) if check_date else date.today()
        except (TypeError, ValueError) as exc:
            raise ProtocolValidationError("check_date must use YYYY-MM-DD") from exc
        normalized_note = str(note or "").strip()
        if len(normalized_note) > 500:
            raise ProtocolValidationError("equipment check note exceeds 500 characters")

        normalized_equipment: Dict[str, Any] = {}
        for field_name in REQUIRED_ANTHROPOMETRY_EQUIPMENT_FIELDS:
            value = str(equipment.get(field_name) or "").strip()
            if not _SAFE_ID_RE.fullmatch(value):
                raise ProtocolValidationError(
                    f"equipment.{field_name} must be a valid equipment ID"
                )
            normalized_equipment[field_name] = value
        if equipment.get("equipment_check_confirmed") is not True:
            raise ProtocolValidationError(
                "equipment.equipment_check_confirmed must be true"
            )

        recorded_at = self._now()
        check_id = (
            f"EQ_{recorded_date.strftime('%Y%m%d')}_{operator_id}_"
            f"{uuid.uuid4().hex[:10]}"
        )
        record = {
            "schema_version": "1.0",
            "check_id": check_id,
            "check_date": recorded_date.isoformat(),
            "checked_at": recorded_at,
            "operator_id": operator_id,
            "equipment": {
                **normalized_equipment,
                "equipment_check_confirmed": True,
            },
            "note": normalized_note,
        }
        operator_dir = (
            self.equipment_checks_dir / recorded_date.isoformat() / operator_id
        )
        operator_dir.mkdir(parents=True, exist_ok=True)
        path = operator_dir / f"{check_id}.json"
        self._atomic_write_json(path, record)
        record["sha256"] = self._sha256(path)
        return self._copy_json(record)

    def get_equipment_check(
        self,
        operator_id: str,
        *,
        check_id: Optional[str] = None,
        check_date: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return a verified daily check, defaulting to today's newest record."""

        operator_id = self._validate_safe_id(operator_id, "operator_id")
        if check_id:
            safe_check_id = self._validate_safe_id(check_id, "check_id")
            candidates = sorted(self.equipment_checks_dir.glob(f"*/*/{safe_check_id}.json"))
        else:
            try:
                recorded_date = date.fromisoformat(check_date) if check_date else date.today()
            except (TypeError, ValueError) as exc:
                raise ProtocolValidationError("check_date must use YYYY-MM-DD") from exc
            candidates = sorted(
                (self.equipment_checks_dir / recorded_date.isoformat() / operator_id).glob("EQ_*.json")
            )
        valid_records = []
        for path in candidates:
            record = self._read_json(path)
            if record.get("operator_id") == operator_id and (
                not check_id or record.get("check_id") == check_id
            ):
                valid_records.append((str(record.get("checked_at") or ""), path, record))
        if valid_records:
            _, path, record = max(valid_records, key=lambda item: (item[0], str(item[1])))
            record["sha256"] = self._sha256(path)
            return self._copy_json(record)
        return None

    def create_subject(
        self,
        subject_id: str,
        protocol_version: str,
        profile_id: str,
        subject_metadata: Mapping[str, Any],
        expected_conditions: Optional[Iterable[Any]] = None,
        capture_policy_version: str = CAPTURE_POLICY_VERSION,
        capture_policy: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a new subject exactly once.

        Existing subject paths are rejected even if they look empty.  This is
        intentional: reuse could silently mix two people or two protocol runs.
        """

        subject_id = self._validate_safe_id(subject_id, "subject_id")
        protocol_version = self._validate_nonempty_text(protocol_version, "protocol_version")
        profile_id = self._validate_safe_id(profile_id, "profile_id")
        metadata = self._json_ready(subject_metadata, "subject_metadata")
        if not isinstance(metadata, dict):
            raise ProtocolValidationError("subject_metadata must be a mapping")

        capture_policy_version = self._validate_nonempty_text(
            capture_policy_version, "capture_policy_version"
        )
        expected_ids, condition_snapshot = self._resolve_expected_conditions(
            profile_id, expected_conditions
        )
        subject_dir = self._subject_dir(subject_id)

        with self._lock_for(subject_id):
            if self._manifest_path(subject_id).exists():
                raise SubjectExistsError(
                    f"subject manifest already exists and cannot be reused: {subject_id}"
                )
            try:
                subject_dir.mkdir(parents=False, exist_ok=False)
            except FileExistsError as exc:
                raise SubjectExistsError(f"subject already exists: {subject_id}") from exc

            try:
                meta_dir = subject_dir / "meta"
                meta_dir.mkdir()
                (subject_dir / ".staging").mkdir()
                protocol_snapshot = self._build_protocol_snapshot(
                    protocol_version=protocol_version,
                    profile_id=profile_id,
                    conditions=condition_snapshot,
                    capture_policy_version=capture_policy_version,
                    capture_policy=capture_policy,
                )
                snapshot_path = meta_dir / "protocol_snapshot.json"
                self._atomic_write_json(snapshot_path, protocol_snapshot)
                snapshot_file_sha256 = self._sha256(snapshot_path)
                conditions = {
                    condition_id: {
                        "status": "PENDING",
                        "attempt_ids": [],
                        "accepted_attempt_id": None,
                    }
                    for condition_id in expected_ids
                }
                now = self._now()
                state: Dict[str, Any] = {
                    "schema_version": "1.0",
                    "revision": 1,
                    "subject_id": subject_id,
                    "dataset_phase": self.dataset_phase,
                    "protocol_version": protocol_version,
                    "profile_id": profile_id,
                    "capture_policy_version": capture_policy_version,
                    "subject_metadata": metadata,
                    "status": "ACTIVE",
                    "created_at": now,
                    "completed_at": None,
                    "expected_condition_ids": expected_ids,
                    "protocol_snapshot": protocol_snapshot,
                    "protocol_snapshot_file": {
                        "path": "meta/protocol_snapshot.json",
                        "sha256": snapshot_file_sha256,
                    },
                    "conditions": conditions,
                    "attempts": {},
                    "anthropometry": {
                        "status": "MISSING",
                        "revision_count": 0,
                        "latest_revision": None,
                        "latest_path": None,
                    },
                }
                self._atomic_write_json(self._state_path(subject_id), state)
                self._atomic_append_jsonl(
                    self._manifest_path(subject_id),
                    {
                        "event": "SUBJECT_CREATED",
                        "timestamp": now,
                        "subject_id": subject_id,
                        "dataset_phase": self.dataset_phase,
                        "protocol_version": protocol_version,
                        "profile_id": profile_id,
                        "expected_condition_ids": expected_ids,
                        "protocol_snapshot_path": "meta/protocol_snapshot.json",
                        "protocol_snapshot_sha256": snapshot_file_sha256,
                        "protocol_snapshot_content_sha256": protocol_snapshot["sha256"],
                    },
                )
                return self._copy_json(state)
            except Exception:
                # Only a just-created, uncommitted directory is removed.  No
                # pre-existing subject data can reach this branch.
                shutil.rmtree(subject_dir, ignore_errors=True)
                manifest = self._manifest_path(subject_id)
                if manifest.exists():
                    try:
                        manifest.unlink()
                    except OSError:
                        pass
                raise

    def get_subject_state(self, subject_id: str) -> Dict[str, Any]:
        subject_id = self._validate_safe_id(subject_id, "subject_id")
        with self._lock_for(subject_id):
            state = self._read_state(subject_id)
            if self._replayable_completion_event(state) is not None:
                self.reconcile_subject(subject_id, verify_committed=False)
                state = self._read_state(subject_id)
            return self._state_with_anthropometry(state)

    def list_subjects(self) -> List[Dict[str, Any]]:
        """Return stable subject summaries sorted by creation time and ID."""

        summaries: List[Dict[str, Any]] = []
        if not self.subjects_dir.exists():
            return summaries
        for subject_dir in self.subjects_dir.iterdir():
            if not subject_dir.is_dir() or not _SAFE_ID_RE.fullmatch(subject_dir.name):
                continue
            subject_id = subject_dir.name
            with self._lock_for(subject_id):
                try:
                    state = self._read_state(subject_id)
                    conditions = state.get("conditions", {})
                    if not isinstance(conditions, Mapping):
                        raise ProtocolStoreError("subject conditions must be a mapping")
                    captured = sum(
                        isinstance(item, Mapping) and item.get("status") == "CAPTURED"
                        for item in conditions.values()
                    )
                    summaries.append(
                        {
                            "subject_id": subject_id,
                            "status": state["status"],
                            "created_at": state["created_at"],
                            "completed_at": state.get("completed_at"),
                            "protocol_version": state["protocol_version"],
                            "profile_id": state["profile_id"],
                            "dataset_phase": state["dataset_phase"],
                            "captured_conditions": captured,
                            "expected_conditions": len(
                                state["expected_condition_ids"]
                            ),
                            "anthropometry_complete": (
                                state.get("anthropometry", {}).get("status")
                                == "COMPLETE"
                            ),
                        }
                    )
                except Exception as exc:
                    summaries.append(
                        {
                            "subject_id": subject_id,
                            "status": "UNREADABLE",
                            "created_at": None,
                            "completed_at": None,
                            "protocol_version": None,
                            "profile_id": None,
                            "dataset_phase": None,
                            "captured_conditions": 0,
                            "expected_conditions": 0,
                            "anthropometry_complete": False,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
        return sorted(
            summaries,
            key=lambda item: (item.get("created_at") is None, item.get("created_at") or "", item["subject_id"]),
        )

    def get_latest_anthropometry(self, subject_id: str) -> Optional[Dict[str, Any]]:
        """Load the latest append-only anthropometry revision, if present."""

        subject_id = self._validate_safe_id(subject_id, "subject_id")
        with self._lock_for(subject_id):
            state = self._read_state(subject_id)
            latest_path = state.get("anthropometry", {}).get("latest_path")
            if not latest_path:
                return None
            path = self._subject_dir(subject_id) / Path(latest_path)
            if not path.is_file():
                raise ProtocolStoreError(
                    f"latest anthropometry revision is missing: {latest_path}"
                )
            expected_hash = state["anthropometry"].get("latest_sha256")
            if expected_hash and self._sha256(path) != expected_hash:
                raise ProtocolStoreError(
                    f"latest anthropometry revision hash mismatch: {latest_path}"
                )
            return self._read_json(path)

    def get_protocol_snapshot(self, subject_id: str) -> Dict[str, Any]:
        """Return and verify the immutable protocol snapshot for a subject."""

        subject_id = self._validate_safe_id(subject_id, "subject_id")
        with self._lock_for(subject_id):
            state = self._read_state(subject_id)
            changed = self._ensure_protocol_snapshot(state)
            if changed:
                self._advance_state(state)
                self._atomic_write_json(self._state_path(subject_id), state)
            snapshot = self._read_protocol_snapshot_file(state)
            return self._copy_json(snapshot)

    def get_verified_anchor_files(
        self,
        subject_id: str,
        condition_id: str,
        attempt_id: str,
        frame_index: int = 3,
        modalities: Sequence[str] = ("rgb", "depth_aligned"),
    ) -> Dict[str, Any]:
        """Return bytes only after verifying the full durable attempt tree.

        Paths are selected from immutable ``capture.json`` rather than from
        mutable subject state.  ``evidence_sha256`` binds the attempt identity,
        selected frame files, and all three sidecar hashes so callers can
        re-verify evidence immediately before recording an ACCEPT decision.
        """

        subject_id = self._validate_safe_id(subject_id, "subject_id")
        condition = self._normalize_condition({"condition_id": condition_id})
        attempt_id = self._validate_nonempty_text(attempt_id, "attempt_id")
        if not isinstance(frame_index, int) or not 1 <= frame_index <= FRAME_COUNT:
            raise ProtocolValidationError(
                f"frame_index must be between 1 and {FRAME_COUNT}"
            )
        if not isinstance(modalities, Sequence) or isinstance(modalities, (str, bytes)):
            raise ProtocolValidationError("modalities must be a sequence")
        requested_modalities = [str(item) for item in modalities]
        allowed = {"rgb", "depth_raw", "depth_aligned", "ir", "ir_left", "ir_right"}
        if (
            not requested_modalities
            or len(requested_modalities) != len(set(requested_modalities))
            or set(requested_modalities) - allowed
        ):
            raise ProtocolValidationError("modalities contain duplicates or invalid values")

        with self._lock_for(subject_id):
            state = self._read_state(subject_id)
            attempt = state.get("attempts", {}).get(attempt_id)
            if not isinstance(attempt, Mapping):
                raise ProtocolValidationError("attempt does not exist in subject state")
            if attempt.get("condition_id") != condition["condition_id"]:
                raise ProtocolValidationError("attempt does not belong to condition")
            if attempt.get("status") != "COMMITTED":
                raise ProtocolValidationError("attempt is not COMMITTED")
            attempt_condition = attempt.get("condition")
            if not isinstance(attempt_condition, Mapping):
                raise ProtocolStoreError("attempt has no canonical condition")
            final_dir = self._attempt_final_dir(
                subject_id,
                attempt_condition,
                attempt_id,
            )
            durable = self._verify_attempt_directory(
                final_dir,
                expected_attempt_id=attempt_id,
                expected_condition_id=condition["condition_id"],
                staging=False,
                subject_state=state,
            )
            for immutable_field in (
                "files",
                "frames",
                "qc",
                "camera_metadata",
                "condition",
                "quality_status",
                "sidecars",
            ):
                if attempt.get(immutable_field) != durable.get(immutable_field):
                    raise ProtocolStoreError(
                        f"state {immutable_field} differs from verified durable attempt"
                    )

            selected: Dict[str, Dict[str, Any]] = {}
            for modality in requested_modalities:
                matches = [
                    item
                    for item in durable["files"]
                    if int(item.get("frame_index", -1)) == frame_index
                    and item.get("modality") == modality
                ]
                if len(matches) != 1:
                    raise ProtocolStoreError(
                        f"verified attempt has {len(matches)} {modality} files for "
                        f"F{frame_index:02d}"
                    )
                record = matches[0]
                absolute_path = (self.base_dir / Path(record["path"])).resolve()
                try:
                    absolute_path.relative_to(final_dir.resolve())
                except ValueError as exc:
                    raise ProtocolStoreError(
                        "anchor file escapes the expected attempt directory"
                    ) from exc
                payload = absolute_path.read_bytes()
                if hashlib.sha256(payload).hexdigest() != record["sha256"]:
                    raise ProtocolStoreError("anchor file changed during evidence read")
                selected[modality] = {
                    "path": str(absolute_path),
                    "relative_path": str(record["path"]),
                    "size_bytes": len(payload),
                    "sha256": record["sha256"],
                    "bytes": payload,
                }
            evidence_payload = {
                "subject_id": subject_id,
                "condition_id": condition["condition_id"],
                "attempt_id": attempt_id,
                "frame_index": frame_index,
                "files": {
                    name: {
                        "relative_path": item["relative_path"],
                        "size_bytes": item["size_bytes"],
                        "sha256": item["sha256"],
                    }
                    for name, item in selected.items()
                },
                "sidecars": durable["sidecars"],
            }
            return {
                **evidence_payload,
                "files": selected,
                "camera_metadata": self._copy_json(durable["camera_metadata"]),
                "evidence_sha256": self._canonical_sha256(evidence_payload),
                "verified_at": self._now(),
            }

    def recover(
        self,
        subject_id: Optional[str] = None,
        *,
        verify_committed: bool = True,
        strict: bool = False,
    ) -> Dict[str, Any]:
        """Reconcile durable files, append-only records, and subject state.

        Startup calls this with ``verify_committed=False``: all incomplete
        attempts are still hash-verified, while already reconciled commits are
        not re-hashed on every launch.  An operator/daily-close audit should
        call ``recover(verify_committed=True)``.
        """

        started_at = self._now()
        if subject_id is not None:
            subject_ids = [self._validate_safe_id(subject_id, "subject_id")]
        else:
            subject_ids = sorted(
                path.name
                for path in self.subjects_dir.iterdir()
                if path.is_dir() and _SAFE_ID_RE.fullmatch(path.name)
            )
        subject_reports = []
        errors = []
        for current_subject_id in subject_ids:
            try:
                subject_reports.append(
                    self.reconcile_subject(
                        current_subject_id,
                        verify_committed=verify_committed,
                    )
                )
            except Exception as exc:
                error = {
                    "subject_id": current_subject_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                errors.append(error)
                if strict:
                    raise
        return {
            "started_at": started_at,
            "completed_at": self._now(),
            "verify_committed": verify_committed,
            "subjects_scanned": len(subject_ids),
            "subjects_changed": sum(bool(item.get("changed")) for item in subject_reports),
            "recovered_commits": sum(item.get("recovered_commits", 0) for item in subject_reports),
            "aborted_attempts": sum(item.get("aborted_attempts", 0) for item in subject_reports),
            "write_failed_attempts": sum(
                item.get("write_failed_attempts", 0) for item in subject_reports
            ),
            "recovered_anthropometry": sum(
                bool(item.get("recovered_anthropometry")) for item in subject_reports
            ),
            "subjects": subject_reports,
            "errors": errors,
        }

    def reconcile_subject(
        self,
        subject_id: str,
        *,
        verify_committed: bool = True,
    ) -> Dict[str, Any]:
        """Reconcile one subject without deleting staging or final evidence."""

        subject_id = self._validate_safe_id(subject_id, "subject_id")
        with self._lock_for(subject_id):
            state = self._read_state(subject_id)
            actions: List[Dict[str, Any]] = []
            audit_errors: List[str] = []
            changed = self._ensure_protocol_snapshot(state)
            recovered_commits = 0
            aborted_attempts = 0
            write_failed_attempts = 0

            final_dirs = {
                path.name: path
                for path in self._subject_dir(subject_id).glob(
                    "cameras/*/conditions/*/attempts/*"
                )
                if path.is_dir()
            }
            staging_root = self._subject_dir(subject_id) / ".staging"
            staging_root.mkdir(parents=True, exist_ok=True)
            staging_dirs = {
                path.name: path for path in staging_root.iterdir() if path.is_dir()
            }

            # Reconcile all state-known attempts first.
            for attempt_id, attempt in list(state.get("attempts", {}).items()):
                status = attempt.get("status")
                candidate = final_dirs.get(attempt_id)
                candidate_is_staging = False
                if candidate is None:
                    candidate = staging_dirs.get(attempt_id)
                    candidate_is_staging = candidate is not None
                if status in {"PENDING", "ABORTED", "WRITE_FAILED"}:
                    if candidate is None:
                        if status == "PENDING":
                            self._mark_recovered_incomplete_attempt(
                                state,
                                attempt,
                                status="ABORTED",
                                error="startup recovery: pending attempt has no durable commit",
                            )
                            aborted_attempts += 1
                            changed = True
                            actions.append(
                                {"attempt_id": attempt_id, "action": "ABORTED"}
                            )
                        continue
                    try:
                        durable = self._verify_attempt_directory(
                            candidate,
                            expected_attempt_id=attempt_id,
                            expected_condition_id=attempt.get("condition_id"),
                            staging=candidate_is_staging,
                            subject_state=state,
                        )
                        if candidate_is_staging:
                            final_dir = self._attempt_final_dir(
                                subject_id,
                                durable["condition"],
                                attempt_id,
                            )
                            if final_dir.exists():
                                raise ProtocolStoreError(
                                    "both staging and final attempt directories exist"
                                )
                            final_dir.parent.mkdir(parents=True, exist_ok=True)
                            self._promote_attempt_directory(candidate, final_dir)
                            final_dirs[attempt_id] = final_dir
                        self._promote_durable_attempt(state, durable)
                        recovered_commits += 1
                        changed = True
                        actions.append(
                            {
                                "attempt_id": attempt_id,
                                "condition_id": durable.get("condition_id"),
                                "quality_status": durable.get("quality_status"),
                                "sidecars": durable.get("sidecars"),
                                "action": "COMMITTED",
                            }
                        )
                    except Exception as exc:
                        if status == "PENDING":
                            self._mark_recovered_incomplete_attempt(
                                state,
                                attempt,
                                status="WRITE_FAILED",
                                error=f"startup recovery: {type(exc).__name__}: {exc}",
                            )
                            write_failed_attempts += 1
                            changed = True
                        audit_errors.append(f"{attempt_id}: {exc}")
                        actions.append(
                            {
                                "attempt_id": attempt_id,
                                "action": (
                                    "WRITE_FAILED"
                                    if status == "PENDING"
                                    else "INVALID_DURABLE_EVIDENCE_IGNORED"
                                ),
                            }
                        )
                elif status == "COMMITTED" and verify_committed:
                    if candidate is None or candidate_is_staging:
                        self._mark_recovered_incomplete_attempt(
                            state,
                            attempt,
                            status="WRITE_FAILED",
                            error="full audit: committed attempt final directory is missing",
                        )
                        write_failed_attempts += 1
                        changed = True
                        audit_errors.append(f"{attempt_id}: final directory missing")
                        continue
                    try:
                        durable = self._verify_attempt_directory(
                            candidate,
                            expected_attempt_id=attempt_id,
                            expected_condition_id=attempt.get("condition_id"),
                            staging=False,
                            subject_state=state,
                        )
                        # State and capture.json must describe the same durable
                        # file set.  Recovery never silently substitutes data.
                        if durable.get("files") != attempt.get("files"):
                            raise ProtocolStoreError(
                                "state file records differ from durable capture.json"
                            )
                        for immutable_field in (
                            "frames",
                            "qc",
                            "camera_metadata",
                            "quality_status",
                            "condition",
                            "sidecars",
                        ):
                            if durable.get(immutable_field) != attempt.get(immutable_field):
                                raise ProtocolStoreError(
                                    f"state {immutable_field} differs from durable sidecars"
                                )
                    except Exception as exc:
                        self._mark_recovered_incomplete_attempt(
                            state,
                            attempt,
                            status="WRITE_FAILED",
                            error=f"full audit: {type(exc).__name__}: {exc}",
                        )
                        write_failed_attempts += 1
                        changed = True
                        audit_errors.append(f"{attempt_id}: {exc}")

            # A crash may leave a final directory after os.replace but before
            # the attempt reached subject_state.json.  Import valid orphans.
            for attempt_id, directory in final_dirs.items():
                if attempt_id in state.get("attempts", {}):
                    continue
                try:
                    durable = self._verify_attempt_directory(
                        directory,
                        expected_attempt_id=attempt_id,
                        staging=False,
                        subject_state=state,
                    )
                    self._promote_durable_attempt(state, durable)
                    recovered_commits += 1
                    changed = True
                    actions.append(
                        {
                            "attempt_id": attempt_id,
                            "condition_id": durable.get("condition_id"),
                            "quality_status": durable.get("quality_status"),
                            "sidecars": durable.get("sidecars"),
                            "action": "IMPORTED_COMMIT",
                        }
                    )
                except Exception as exc:
                    audit_errors.append(f"orphan final {attempt_id}: {exc}")

            # Durable manual reviews are replayable after a crash.
            reviews_root = self._subject_dir(subject_id) / "meta" / "reviews"
            if reviews_root.exists():
                for review_path in sorted(reviews_root.glob("*/*.json")):
                    try:
                        review = self._read_json(review_path)
                        expected_file_hash = self._sha256(review_path)
                        attempt = state.get("attempts", {}).get(review.get("attempt_id"))
                        if not isinstance(attempt, MutableMapping):
                            raise ProtocolStoreError("review references missing attempt")
                        existing = attempt.get("review")
                        if isinstance(existing, Mapping):
                            if existing.get("review_id") != review.get("review_id"):
                                raise ProtocolStoreError(
                                    "multiple review decisions exist for one attempt"
                                )
                            continue
                        reference = {
                            **review,
                            "path": review_path.relative_to(
                                self._subject_dir(subject_id)
                            ).as_posix(),
                            "file_sha256": expected_file_hash,
                        }
                        self._apply_review_to_state(state, reference)
                        changed = True
                        actions.append(
                            {
                                "attempt_id": review.get("attempt_id"),
                                "review_id": review.get("review_id"),
                                "review_file_sha256": expected_file_hash,
                                "decision": review.get("decision"),
                                "review_status": review.get("review_status"),
                                "content_sha256": review.get("content_sha256"),
                                "action": "REVIEW_REPLAYED",
                            }
                        )
                    except Exception as exc:
                        audit_errors.append(
                            f"review {review_path.name}: {type(exc).__name__}: {exc}"
                        )

            recovered_anthropometry, anthro_errors = self._reconcile_anthropometry(
                state
            )
            if recovered_anthropometry:
                changed = True
                actions.append(
                    {
                        "action": "ANTHROPOMETRY_RECOVERED",
                        "revision": state.get("anthropometry", {}).get(
                            "latest_revision"
                        ),
                        "path": state.get("anthropometry", {}).get("latest_path"),
                        "sha256": state.get("anthropometry", {}).get(
                            "latest_sha256"
                        ),
                    }
                )
            audit_errors.extend(anthro_errors)

            if self._rebuild_condition_states(state):
                changed = True
            completion_event = self._replayable_completion_event(state)
            if completion_event is not None:
                preliminary_completion = self._build_completion_report(state)
                if preliminary_completion.get("ready_to_complete"):
                    state["status"] = "COMPLETE"
                    state["completed_at"] = completion_event["timestamp"]
                    changed = True
                    actions.append(
                        {
                            "action": "SUBJECT_COMPLETION_REPLAYED",
                            "completion_event_index": completion_event.get(
                                "event_index"
                            ),
                            "state_revision": completion_event.get(
                                "state_revision"
                            ),
                        }
                    )
                else:
                    audit_errors.append(
                        "durable SUBJECT_COMPLETED event could not be replayed: "
                        "current subject integrity gate is not ready"
                    )
            if changed:
                self._advance_state(state)
                recovery_at = self._now()
                self._atomic_append_jsonl(
                    self._manifest_path(subject_id),
                    {
                        "event": "SUBJECT_RECONCILED",
                        "timestamp": recovery_at,
                        "subject_id": subject_id,
                        "actions": actions,
                        "audit_errors": audit_errors,
                    },
                )
                self._atomic_write_json(self._state_path(subject_id), state)

            report_rebuilt = self._repair_completion_report(state)
            return {
                "subject_id": subject_id,
                "changed": changed or report_rebuilt,
                "recovered_commits": recovered_commits,
                "aborted_attempts": aborted_attempts,
                "write_failed_attempts": write_failed_attempts,
                "recovered_anthropometry": recovered_anthropometry,
                "completion_report_rebuilt": report_rebuilt,
                "actions": actions,
                "audit_errors": audit_errors,
            }

    def begin_capture_attempt(
        self,
        subject_id: str,
        condition_dict: Mapping[str, Any] | Any,
        retake_reason: Optional[str] = None,
        target_attempt_id: Optional[str] = None,
        invalidate_prior: Optional[bool] = None,
    ) -> str:
        """Reserve a unique capture attempt and return its attempt ID."""

        subject_id = self._validate_safe_id(subject_id, "subject_id")
        condition = self._normalize_condition(condition_dict)
        raw_condition = self._as_mapping(condition_dict, "condition")
        retake_reason = retake_reason or raw_condition.get("retake_reason")
        target_attempt_id = target_attempt_id or raw_condition.get("target_attempt_id")
        if invalidate_prior is None and "invalidate_prior" in raw_condition:
            invalidate_prior = raw_condition.get("invalidate_prior")
        with self._lock_for(subject_id):
            state = self._read_state(subject_id)
            self._ensure_active(state)
            condition_id = condition["condition_id"]
            if condition_id not in state["conditions"]:
                raise ProtocolValidationError(
                    f"condition is not part of profile {state['profile_id']}: {condition_id}"
                )
            condition_state = state["conditions"][condition_id]
            unresolved_warns = [
                attempt
                for attempt in state.get("attempts", {}).values()
                if attempt.get("condition_id") == condition_id
                and attempt.get("status") == "COMMITTED"
                and attempt.get("quality_status") == "WARN"
                and attempt.get("review_status") in {None, "PENDING"}
            ]
            if condition_state["status"] == "REVIEW_REQUIRED" or unresolved_warns:
                raise ProtocolValidationError(
                    f"condition has a WARN attempt awaiting manual review: {condition_id}"
                )
            for attempt in state["attempts"].values():
                if attempt["condition_id"] == condition_id and attempt["status"] == "PENDING":
                    raise ProtocolValidationError(
                        f"condition already has a pending attempt: {attempt['attempt_id']}"
                    )

            attempt_id = self._new_attempt_id()
            now = self._now()
            prior_accepted_id = condition_state.get("accepted_attempt_id")
            is_accepted_retake = condition_state["status"] == "CAPTURED"
            if is_accepted_retake:
                if not retake_reason or not target_attempt_id or not isinstance(
                    invalidate_prior, bool
                ):
                    raise ProtocolValidationError(
                        "retaking a CAPTURED condition requires retake_reason, "
                        "target_attempt_id, and boolean invalidate_prior"
                    )
                retake_reason = self._validate_nonempty_text(
                    str(retake_reason), "retake_reason"
                )
                target_attempt_id = self._validate_nonempty_text(
                    str(target_attempt_id), "target_attempt_id"
                )
                if target_attempt_id != prior_accepted_id:
                    raise ProtocolValidationError(
                        "retake target_attempt_id must equal current accepted_attempt_id"
                    )
                prior = state["attempts"].get(prior_accepted_id)
                if not isinstance(prior, MutableMapping):
                    raise ProtocolStoreError("accepted attempt is missing from subject state")
                if invalidate_prior:
                    prior["validity"] = "INVALIDATED"
                    prior["review_status"] = "INVALIDATED"
                    prior["invalidation"] = {
                        "invalidated_at": now,
                        "invalidated_by_attempt_id": attempt_id,
                        "reason": retake_reason,
                    }
                    condition_state["accepted_attempt_id"] = None
                else:
                    prior.setdefault("validity", "VALID")
            elif any(
                value is not None
                for value in (retake_reason, target_attempt_id, invalidate_prior)
            ):
                if target_attempt_id:
                    target = state["attempts"].get(str(target_attempt_id))
                    if not isinstance(target, Mapping) or target.get("condition_id") != condition_id:
                        raise ProtocolValidationError(
                            "retake target does not belong to this condition"
                        )
                if retake_reason:
                    retake_reason = self._validate_nonempty_text(
                        str(retake_reason), "retake_reason"
                    )
                if invalidate_prior not in {None, False}:
                    raise ProtocolValidationError(
                        "invalidate_prior is only valid for an accepted attempt"
                    )
            attempt = {
                "attempt_id": attempt_id,
                "condition_id": condition_id,
                "condition": condition,
                "status": "PENDING",
                "started_at": now,
                "committed_at": None,
                "quality_status": None,
                "disposition": None,
                "review_status": None,
                "review": None,
                "files": [],
                "prior_accepted_attempt_id": prior_accepted_id,
                "retake_reason": retake_reason,
                "target_attempt_id": target_attempt_id,
                "invalidate_prior": bool(invalidate_prior),
            }
            state["attempts"][attempt_id] = attempt
            condition_state["attempt_ids"].append(attempt_id)
            condition_state["status"] = "IN_PROGRESS"
            self._advance_state(state)
            self._atomic_append_jsonl(
                self._manifest_path(subject_id),
                {
                    "event": "CAPTURE_ATTEMPT_BEGUN",
                    "timestamp": now,
                    "subject_id": subject_id,
                    "attempt_id": attempt_id,
                    "condition_id": condition_id,
                    "condition": condition,
                    "retake_reason": retake_reason,
                    "target_attempt_id": target_attempt_id,
                    "invalidate_prior": bool(invalidate_prior),
                },
            )
            self._atomic_write_json(self._state_path(subject_id), state)
            return attempt_id

    def fail_capture_attempt(
        self,
        subject_id: str,
        condition_dict: Mapping[str, Any] | Any,
        reason: str,
    ) -> Dict[str, Any]:
        """Abort the unique pending attempt without deleting any history."""

        subject_id = self._validate_safe_id(subject_id, "subject_id")
        condition = self._normalize_condition(condition_dict)
        reason = self._validate_nonempty_text(reason, "reason")
        with self._lock_for(subject_id):
            state = self._read_state(subject_id)
            self._ensure_active(state)
            pending = [
                item
                for item in state["attempts"].values()
                if item["condition_id"] == condition["condition_id"]
                and item["status"] == "PENDING"
            ]
            requested_attempt_id = self._extract_attempt_id(condition_dict)
            if requested_attempt_id:
                pending = [
                    item for item in pending if item["attempt_id"] == requested_attempt_id
                ]
            if len(pending) != 1:
                raise ProtocolValidationError(
                    "exactly one pending attempt is required to abort; "
                    f"found {len(pending)}"
                )
            attempt = pending[0]
            self._assert_condition_compatible(attempt["condition"], condition)
            final_dir = self._attempt_final_dir(
                subject_id,
                attempt["condition"],
                attempt["attempt_id"],
            )
            if final_dir.is_dir():
                durable = self._verify_attempt_directory(
                    final_dir,
                    expected_attempt_id=attempt["attempt_id"],
                    expected_condition_id=attempt["condition_id"],
                    staging=False,
                    subject_state=state,
                )
                self._promote_durable_attempt(state, durable)
                self._rebuild_condition_states(state)
                self._advance_state(state)
                self._atomic_append_jsonl(
                    self._manifest_path(subject_id),
                    {
                        "event": "SUBJECT_RECONCILED",
                        "timestamp": self._now(),
                        "subject_id": subject_id,
                        "actions": [
                            {
                                "action": "COMMITTED",
                                "attempt_id": durable["attempt_id"],
                                "condition_id": durable["condition_id"],
                                "quality_status": durable["quality_status"],
                                "sidecars": durable["sidecars"],
                            }
                        ],
                        "audit_errors": [],
                    },
                )
                self._atomic_write_json(self._state_path(subject_id), state)
                raise ProtocolValidationError(
                    "cannot abort: a valid durable final commit already exists"
                )
            now = self._now()
            attempt["status"] = "ABORTED"
            attempt["committed_at"] = now
            attempt["quality_status"] = "FAIL"
            attempt["disposition"] = "BAD"
            attempt["review_status"] = "NOT_APPLICABLE"
            attempt["error"] = reason
            condition_state = state["conditions"][attempt["condition_id"]]
            condition_state["status"] = (
                "CAPTURED"
                if condition_state.get("accepted_attempt_id")
                else "NEEDS_RETAKE"
            )
            self._advance_state(state)
            self._atomic_append_jsonl(
                self._manifest_path(subject_id),
                {
                    "event": "CAPTURE_ATTEMPT_ABORTED",
                    "timestamp": now,
                    "subject_id": subject_id,
                    "attempt_id": attempt["attempt_id"],
                    "condition_id": attempt["condition_id"],
                    "reason": reason,
                },
            )
            self._atomic_write_json(self._state_path(subject_id), state)
            return self._copy_json(attempt)

    def commit_capture_attempt(
        self,
        subject_id: str,
        condition_dict: Mapping[str, Any] | Any,
        frames: Any,
        qc: Mapping[str, Any],
        camera_metadata: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Atomically persist a five-frame burst for a pending attempt.

        Required per-frame modalities are RGB, raw depth, and RGB-aligned
        depth.  One or two IR streams are saved when supplied.  A failed QC
        attempt is still committed and marked ``BAD``; a later successful
        attempt is marked ``RETAKE``.  Neither is overwritten.
        """

        subject_id = self._validate_safe_id(subject_id, "subject_id")
        supplied_condition = self._normalize_condition(condition_dict)

        with self._lock_for(subject_id):
            state = self._read_state(subject_id)
            self._ensure_active(state)
            condition_id = supplied_condition["condition_id"]
            pending = [
                item
                for item in state["attempts"].values()
                if item["condition_id"] == condition_id and item["status"] == "PENDING"
            ]
            requested_attempt_id = self._extract_attempt_id(condition_dict)
            if requested_attempt_id:
                pending = [item for item in pending if item["attempt_id"] == requested_attempt_id]
            if len(pending) != 1:
                raise ProtocolValidationError(
                    f"exactly one pending attempt is required for {condition_id}; found {len(pending)}"
                )
            attempt = pending[0]
            try:
                self._assert_condition_compatible(attempt["condition"], supplied_condition)
                normalized_frames = self._normalize_frames(frames)
                qc_json = self._json_ready(qc, "qc")
                camera_json = self._json_ready(camera_metadata, "camera_metadata")
                if not isinstance(qc_json, dict) or not isinstance(camera_json, dict):
                    raise ProtocolValidationError("qc and camera_metadata must be mappings")
                self._enforce_subject_camera_fingerprint(
                    state,
                    supplied_condition,
                    camera_json,
                )
                self._validate_burst_consistency(normalized_frames, camera_json)
                self._validate_required_modalities(
                    normalized_frames,
                    self._required_modalities_from_state(state),
                )
                self._validate_qc_contract(
                    state,
                    supplied_condition,
                    qc_json,
                    camera_json,
                )
                quality_status = self._quality_status(qc_json)
            except Exception as exc:
                # Once commit has been attempted, malformed/incomplete input is
                # itself an auditable failed attempt.  It must never leave a
                # permanent PENDING reservation that blocks the operator.
                self._record_write_failure(state, attempt, exc)
                raise

            previous = [
                state["attempts"][attempt_id]
                for attempt_id in state["conditions"][condition_id]["attempt_ids"]
                if attempt_id != attempt["attempt_id"]
                and state["attempts"][attempt_id]["status"]
                in {"COMMITTED", "WRITE_FAILED", "ABORTED"}
            ]
            if quality_status == "FAIL":
                bad_number = 1 + sum(item.get("disposition") == "BAD" for item in previous)
                disposition = "BAD"
            elif previous:
                retake_number = 1 + sum(item.get("disposition") == "RETAKE" for item in previous)
                disposition = "RETAKE"
            else:
                disposition = "PRIMARY"

            canonical_condition = attempt["condition"]
            camera_slug = self._camera_slug(canonical_condition["camera_code"])
            final_dir = (
                self._subject_dir(subject_id)
                / "cameras"
                / camera_slug
                / "conditions"
                / condition_id
                / "attempts"
                / attempt["attempt_id"]
            )
            staging_dir = self._subject_dir(subject_id) / ".staging" / attempt["attempt_id"]
            if final_dir.exists() or staging_dir.exists():
                raise ProtocolStoreError(f"attempt storage path already exists: {attempt['attempt_id']}")
            staging_dir.mkdir(parents=True, exist_ok=False)

            try:
                file_records, frame_records = self._write_burst(
                    staging_dir=staging_dir,
                    final_dir=final_dir,
                    subject_id=subject_id,
                    condition=canonical_condition,
                    frames=normalized_frames,
                    camera_metadata=camera_json,
                )
                committed_at = self._now()
                durable_attempt = {
                    **attempt,
                    "commit_record_version": "1.0",
                    "subject_id": subject_id,
                    "status": "COMMITTED",
                    "committed_at": committed_at,
                    "quality_status": quality_status,
                    "disposition": disposition,
                    "review_status": (
                        "NOT_REQUIRED"
                        if quality_status == "PASS"
                        else "PENDING"
                        if quality_status == "WARN"
                        else "NOT_APPLICABLE"
                    ),
                    "review": None,
                    "validity": "VALID",
                    "supersedes_attempt_id": (
                        attempt.get("target_attempt_id")
                        if quality_status == "PASS"
                        else None
                    ),
                    "qc": qc_json,
                    "camera_metadata": camera_json,
                    "files": file_records,
                    "frames": frame_records,
                    "anchor_frame": "F03",
                }
                capture_path = staging_dir / "capture.json"
                qc_path = staging_dir / "qc.json"
                commit_path = staging_dir / COMMIT_RECORD_FILENAME
                self._atomic_write_json(capture_path, durable_attempt)
                self._atomic_write_json(qc_path, qc_json)
                embedded_sidecars = {
                    "capture.json": self._file_integrity_record(capture_path),
                    "qc.json": self._file_integrity_record(qc_path),
                }
                # Written last inside staging and moved with the attempt tree;
                # completion only trusts attempts with this durable marker.
                self._atomic_write_json(
                    commit_path,
                    {
                        "schema_version": "1.0",
                        "attempt_id": attempt["attempt_id"],
                        "condition_id": condition_id,
                        "status": "COMMITTED",
                        "quality_status": quality_status,
                        "review_status": durable_attempt["review_status"],
                        "committed_at": committed_at,
                        "file_count": len(file_records),
                        "files": file_records,
                        "sidecars": embedded_sidecars,
                    },
                )
                durable_attempt["sidecars"] = {
                    **embedded_sidecars,
                    COMMIT_RECORD_FILENAME: self._file_integrity_record(commit_path),
                }
                final_dir.parent.mkdir(parents=True, exist_ok=True)
                self._promote_attempt_directory(staging_dir, final_dir)
            except Exception as exc:
                # Preserve partial staging data.  Recovery will verify it and
                # keep an audit trail instead of silently deleting evidence.
                self._record_write_failure(state, attempt, exc)
                raise

            # The capture directory is now durable.  Bookkeeping failures
            # cannot roll it back and must never be reported as a failed
            # capture; startup recovery will replay the verified commit.
            try:
                attempt.clear()
                attempt.update(durable_attempt)
                if quality_status == "PASS":
                    state["conditions"][condition_id][
                        "accepted_attempt_id"
                    ] = attempt["attempt_id"]
                self._rebuild_condition_states(state)
                self._advance_state(state)
                self._atomic_append_jsonl(
                    self._manifest_path(subject_id),
                    {
                        "event": "CAPTURE_ATTEMPT_COMMITTED",
                        "timestamp": durable_attempt["committed_at"],
                        "subject_id": subject_id,
                        **durable_attempt,
                    },
                )
                self._atomic_write_json(self._state_path(subject_id), state)
                return self._copy_json(durable_attempt)
            except Exception as exc:
                result = self._copy_json(durable_attempt)
                result["bookkeeping_status"] = "PENDING_RECONCILE"
                result["post_commit_error"] = f"{type(exc).__name__}: {exc}"
                try:
                    recovery = self.reconcile_subject(
                        subject_id,
                        verify_committed=False,
                    )
                    recovered_state = self._read_state(subject_id)
                    recovered_attempt = recovered_state.get("attempts", {}).get(
                        durable_attempt["attempt_id"], {}
                    )
                    if recovered_attempt.get("status") == "COMMITTED":
                        result = self._copy_json(recovered_attempt)
                        result["bookkeeping_status"] = "RECOVERED"
                        result["post_commit_error"] = (
                            f"{type(exc).__name__}: {exc}"
                        )
                        result["recovery_report"] = recovery
                except Exception as recovery_exc:
                    result["recovery_error"] = (
                        f"{type(recovery_exc).__name__}: {recovery_exc}"
                    )
                return result

    def review_capture_attempt(
        self,
        subject_id: str,
        condition_dict: Mapping[str, Any] | Any,
        attempt_id: str,
        decision: str,
        reviewer_id: str,
        reason: str,
        policy: Optional[Mapping[str, Any] | str] = None,
    ) -> Dict[str, Any]:
        """Accept or reject one committed WARN attempt exactly once.

        PASS attempts never enter this workflow.  An accepted PASS can neither
        be replaced nor shadowed by review.  Review decisions are separately
        persisted as append-only records so a crash between decision write and
        state update can be reconciled at startup.
        """

        subject_id = self._validate_safe_id(subject_id, "subject_id")
        condition = self._normalize_condition(condition_dict)
        attempt_id = self._validate_nonempty_text(attempt_id, "attempt_id")
        decision = str(decision).strip().upper()
        if decision not in {"ACCEPT", "REJECT"}:
            raise ProtocolValidationError("review decision must be ACCEPT or REJECT")
        reviewer_id = self._validate_nonempty_text(reviewer_id, "reviewer_id")
        reason = self._validate_nonempty_text(reason, "reason")
        policy_json = self._json_ready(
            policy or {"policy_version": "manual-warn-review-v1.0"},
            "review policy",
        )

        with self._lock_for(subject_id):
            state = self._read_state(subject_id)
            self._ensure_active(state)
            attempt = state.get("attempts", {}).get(attempt_id)
            if not isinstance(attempt, MutableMapping):
                raise ProtocolValidationError(f"capture attempt not found: {attempt_id}")
            if attempt.get("condition_id") != condition["condition_id"]:
                raise ProtocolValidationError("attempt does not belong to supplied condition")
            self._assert_condition_compatible(attempt["condition"], condition)
            if attempt.get("status") != "COMMITTED":
                raise ProtocolValidationError("only a COMMITTED attempt can be reviewed")
            if attempt.get("quality_status") != "WARN":
                raise ProtocolValidationError("only a WARN attempt can be manually reviewed")
            if attempt.get("review_status") not in {None, "PENDING"}:
                raise ProtocolValidationError("WARN attempt has already been reviewed")

            condition_state = state["conditions"][condition["condition_id"]]
            accepted_id = condition_state.get("accepted_attempt_id")
            if accepted_id and accepted_id != attempt_id:
                accepted = state["attempts"].get(accepted_id, {})
                retained_prior = (
                    accepted_id == attempt.get("prior_accepted_attempt_id")
                    and not attempt.get("invalidate_prior")
                    and accepted.get("quality_status") == "PASS"
                    and accepted.get("validity", "VALID") != "INVALIDATED"
                )
                if not retained_prior and accepted.get("quality_status") == "PASS":
                    raise ProtocolValidationError(
                        "manual review cannot overwrite an existing accepted PASS"
                    )
                if not retained_prior:
                    raise ProtocolValidationError(
                        f"condition already accepts another attempt: {accepted_id}"
                    )
            if condition_state.get("status") != "REVIEW_REQUIRED":
                raise ProtocolValidationError(
                    "condition is not awaiting review for this WARN attempt"
                )
            durable_review_dir = (
                self._subject_dir(subject_id) / "meta" / "reviews" / attempt_id
            )
            existing_review_files = sorted(durable_review_dir.glob("*.json"))
            if existing_review_files:
                raise ProtocolValidationError(
                    "a durable review sidecar already exists; run recover before retrying"
                )

            reviewed_at = self._now()
            review_id = f"RV{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_{uuid.uuid4().hex[:8]}"
            review: Dict[str, Any] = {
                "schema_version": "1.0",
                "review_id": review_id,
                "subject_id": subject_id,
                "condition_id": condition["condition_id"],
                "attempt_id": attempt_id,
                "decision": decision,
                "review_status": "ACCEPTED" if decision == "ACCEPT" else "REJECTED",
                "reviewer_id": reviewer_id,
                "reason": reason,
                "policy": policy_json,
                "reviewed_at": reviewed_at,
            }
            review["content_sha256"] = self._canonical_sha256(review)
            review_relative = (
                Path("meta") / "reviews" / attempt_id / f"{review_id}.json"
            )
            review_path = self._subject_dir(subject_id) / review_relative
            if review_path.exists():
                raise ProtocolStoreError(f"review record already exists: {review_id}")
            self._atomic_write_json(review_path, review)
            review_file_sha = self._sha256(review_path)
            review_reference = {
                **review,
                "path": review_relative.as_posix(),
                "file_sha256": review_file_sha,
            }
            try:
                self._apply_review_to_state(state, review_reference)
                self._advance_state(state)
                self._atomic_append_jsonl(
                    self._manifest_path(subject_id),
                    {
                        "event": "CAPTURE_ATTEMPT_REVIEWED",
                        "timestamp": reviewed_at,
                        **review_reference,
                    },
                )
                self._atomic_write_json(self._state_path(subject_id), state)
                return self._copy_json(review_reference)
            except Exception as exc:
                result = self._copy_json(review_reference)
                result["bookkeeping_status"] = "PENDING_RECONCILE"
                result["post_commit_error"] = f"{type(exc).__name__}: {exc}"
                try:
                    recovery = self.reconcile_subject(
                        subject_id,
                        verify_committed=False,
                    )
                    recovered = self._read_state(subject_id).get("attempts", {}).get(
                        attempt_id, {}
                    )
                    if recovered.get("review", {}).get("review_id") == review_id:
                        result = self._copy_json(recovered["review"])
                        result["bookkeeping_status"] = "RECOVERED"
                        result["post_commit_error"] = f"{type(exc).__name__}: {exc}"
                        result["recovery_report"] = recovery
                except Exception as recovery_exc:
                    result["recovery_error"] = (
                        f"{type(recovery_exc).__name__}: {recovery_exc}"
                    )
                return result

    # ------------------------------------------------------------------
    # Anthropometry and completion
    # ------------------------------------------------------------------
    def save_anthropometry(
        self,
        subject_id: str,
        measurements: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Append a validated anthropometry revision.

        M01, M03, M06, M09 and M12 are mandatory for the current protocol.
        Every supplied field needs two measurements; a third is mandatory when
        the first two exceed the protocol threshold.  All other measurements
        may be omitted or set to ``None``/an empty string.  Frozen legacy
        snapshots continue to enforce their original M01--M13 requirement.
        """

        subject_id = self._validate_safe_id(subject_id, "subject_id")
        if not isinstance(measurements, Mapping) and not (
            isinstance(measurements, Sequence) and not isinstance(measurements, (str, bytes))
        ):
            raise ProtocolValidationError("measurements must be a mapping or record sequence")
        metadata_json = self._json_ready(metadata or {}, "anthropometry metadata")
        with self._lock_for(subject_id):
            state = self._read_state(subject_id)
            self._ensure_active(state)
            recovered_anthro, recovery_errors = self._reconcile_anthropometry(state)
            if recovered_anthro:
                self._advance_state(state)
                recovery_at = self._now()
                self._atomic_append_jsonl(
                    self._manifest_path(subject_id),
                    {
                        "event": "SUBJECT_RECONCILED",
                        "timestamp": recovery_at,
                        "subject_id": subject_id,
                        "actions": [
                            {
                                "action": "ANTHROPOMETRY_RECOVERED",
                                "revision": state["anthropometry"]["latest_revision"],
                                "path": state["anthropometry"]["latest_path"],
                                "sha256": state["anthropometry"]["latest_sha256"],
                            }
                        ],
                        "audit_errors": recovery_errors,
                    },
                )
                self._atomic_write_json(self._state_path(subject_id), state)
            measurement_definitions = self._measurement_definitions_from_state(state)
            normalized, records = self._normalize_anthropometry(
                measurements,
                definitions=measurement_definitions,
            )
            operator_id = self._validate_anthropometry_metadata(
                state,
                metadata_json,
            )
            metadata_json["operator_id"] = operator_id
            revision = int(state["anthropometry"].get("revision_count", 0)) + 1
            relative_path = Path("meta") / "anthropometry" / f"anthropometry_{revision:04d}.json"
            absolute_path = self._subject_dir(subject_id) / relative_path
            if absolute_path.exists():
                raise ProtocolStoreError(f"anthropometry revision already exists: {revision}")
            record = {
                "schema_version": "1.0",
                "subject_id": subject_id,
                "revision": revision,
                "created_at": self._now(),
                "measurements": normalized,
                "records": records,
                "metadata": metadata_json,
            }
            for measurement_record in record["records"]:
                measurement_record["operator_id"] = operator_id
                measurement_record["recorded_at"] = record["created_at"]
            record["warnings"] = [
                {
                    "measurement_id": item["measurement_id"],
                    "field_name": item["field_name"],
                    "warning": warning,
                }
                for item in record["records"]
                for warning in item.get("warnings", [])
            ]
            record["content_sha256"] = self._canonical_sha256(record)
            self._atomic_write_json(absolute_path, record)
            sha256 = self._sha256(absolute_path)
            try:
                state["anthropometry"] = {
                    "status": "COMPLETE",
                    "revision_count": revision,
                    "latest_revision": revision,
                    "latest_path": relative_path.as_posix(),
                    "latest_sha256": sha256,
                    "completed_measurement_ids": [
                        measurement_id
                        for measurement_id in ALL_MEASUREMENT_IDS
                        if measurement_definitions[measurement_id]["required"]
                    ],
                }
                self._advance_state(state)
                self._atomic_append_jsonl(
                    self._manifest_path(subject_id),
                    {
                        "event": "ANTHROPOMETRY_SAVED",
                        "timestamp": record["created_at"],
                        "subject_id": subject_id,
                        "revision": revision,
                        "path": relative_path.as_posix(),
                        "sha256": sha256,
                    },
                )
                self._atomic_write_json(self._state_path(subject_id), state)
                return self._copy_json(record)
            except Exception as exc:
                result = self._copy_json(record)
                result["bookkeeping_status"] = "PENDING_RECONCILE"
                result["post_commit_error"] = f"{type(exc).__name__}: {exc}"
                try:
                    recovered_state = self._read_state(subject_id)
                    recovered, recovery_errors = self._reconcile_anthropometry(
                        recovered_state
                    )
                    if recovered:
                        self._advance_state(recovered_state)
                        self._atomic_append_jsonl(
                            self._manifest_path(subject_id),
                            {
                                "event": "SUBJECT_RECONCILED",
                                "timestamp": self._now(),
                                "subject_id": subject_id,
                                "actions": [
                                    {
                                        "action": "ANTHROPOMETRY_RECOVERED",
                                        "revision": revision,
                                        "path": relative_path.as_posix(),
                                        "sha256": sha256,
                                    }
                                ],
                                "audit_errors": recovery_errors,
                            },
                        )
                        self._atomic_write_json(
                            self._state_path(subject_id), recovered_state
                        )
                        result["bookkeeping_status"] = "RECOVERED"
                except Exception as recovery_exc:
                    result["recovery_error"] = (
                        f"{type(recovery_exc).__name__}: {recovery_exc}"
                    )
                return result

    def completion_report(self, subject_id: str) -> Dict[str, Any]:
        """Build and atomically store ``subject_completion_report.json``."""

        subject_id = self._validate_safe_id(subject_id, "subject_id")
        with self._lock_for(subject_id):
            state = self._read_state(subject_id)
            report_path = self._completion_report_path(subject_id)
            if state["status"] == "COMPLETE" and report_path.exists():
                fresh = self._build_completion_report(state)
                existing = self._read_json(report_path)
                if fresh["ready_to_complete"] and self._completion_report_is_current(
                    existing, fresh
                ):
                    return existing
                self._atomic_write_json(report_path, fresh)
                return self._copy_json(fresh)
            report = self._build_completion_report(state)
            self._atomic_write_json(report_path, report)
            return self._copy_json(report)

    def complete_subject(self, subject_id: str) -> Dict[str, Any]:
        """Close a subject only after conditions and core GT are complete."""

        subject_id = self._validate_safe_id(subject_id, "subject_id")
        with self._lock_for(subject_id):
            state = self._read_state(subject_id)
            if (
                state.get("status") == "ACTIVE"
                and self._replayable_completion_event(state) is not None
            ):
                self.reconcile_subject(subject_id, verify_committed=False)
                state = self._read_state(subject_id)
            if state["status"] == "COMPLETE":
                report_path = self._completion_report_path(subject_id)
                fresh = self._build_completion_report(state)
                if fresh["ready_to_complete"] and report_path.exists():
                    existing = self._read_json(report_path)
                    if self._completion_report_is_current(existing, fresh):
                        return existing
                self._atomic_write_json(report_path, fresh)
                return self._copy_json(fresh)
            self._ensure_active(state)
            preliminary = self._build_completion_report(state)
            if not preliminary["ready_to_complete"]:
                self._atomic_write_json(self._completion_report_path(subject_id), preliminary)
                raise IncompleteSubjectError(preliminary)

            completed_at = self._now()
            state["status"] = "COMPLETE"
            state["completed_at"] = completed_at
            self._advance_state(state)
            completion_event = {
                "event": "SUBJECT_COMPLETED",
                "timestamp": completed_at,
                "subject_id": subject_id,
                "state_revision": state["revision"],
            }
            try:
                self._atomic_append_jsonl(
                    self._manifest_path(subject_id), completion_event
                )
            except Exception:
                # An injected/OS error may be raised after an atomic replace.
                # Only continue if the exact durable event can be verified.
                try:
                    events = self._read_manifest_events(
                        self._manifest_path(subject_id)
                    )
                except ProtocolStoreError:
                    raise
                if not any(
                    item.get("event") == "SUBJECT_COMPLETED"
                    and item.get("timestamp") == completed_at
                    and item.get("state_revision") == state["revision"]
                    for item in events
                ):
                    raise
            try:
                self._atomic_write_json(self._state_path(subject_id), state)
            except Exception:
                persisted = self._read_state(subject_id)
                if (
                    persisted.get("status") != "COMPLETE"
                    or persisted.get("completed_at") != completed_at
                    or persisted.get("revision") != state["revision"]
                ):
                    raise
                state = persisted
            final_report = self._build_completion_report(state)
            try:
                self._atomic_write_json(
                    self._completion_report_path(subject_id), final_report
                )
                return self._copy_json(final_report)
            except Exception as exc:
                result = self._copy_json(final_report)
                result["bookkeeping_status"] = "REPORT_PENDING_REBUILD"
                result["post_commit_error"] = f"{type(exc).__name__}: {exc}"
                return result

    # ------------------------------------------------------------------
    # Protocol/profile handling
    # ------------------------------------------------------------------
    def _build_protocol_snapshot(
        self,
        protocol_version: str,
        profile_id: str,
        conditions: Sequence[Mapping[str, Any]],
        capture_policy_version: str,
        capture_policy: Optional[Mapping[str, Any]] = None,
        *,
        reconstructed_from_subject_state: bool = False,
    ) -> Dict[str, Any]:
        measurement_snapshot: List[Dict[str, Any]] = []
        shared = globals().get("_SHARED_MEASUREMENTS", {})
        for measurement_id in ALL_MEASUREMENT_IDS:
            local = _MEASUREMENT_DEFINITIONS[measurement_id]
            definition = shared.get(measurement_id) if isinstance(shared, Mapping) else None
            item: Dict[str, Any] = {
                "measurement_id": measurement_id,
                "field_names": list(local["fields"]),
                "unit": local["unit"],
                "required": bool(local["required"]),
                "minimum_repeats": int(getattr(definition, "minimum_repeats", 2)),
                "third_measurement_threshold": local["threshold"],
                "valid_range": (
                    [80.0, 250.0]
                    if measurement_id == "M01"
                    else [20.0, 400.0]
                    if measurement_id == "M02"
                    else [10.0, 100.0]
                    if measurement_id == "M03"
                    else [10.0, 300.0]
                ),
                "repeat_warning_threshold": 0.5 if measurement_id == "M02" else None,
            }
            item["hard_range"] = list(item["valid_range"])
            for attribute in (
                "display_name_zh",
                "kind",
                "protocol_note",
                "required_equipment",
            ):
                if definition is not None and hasattr(definition, attribute):
                    item[attribute] = self._json_ready(
                        getattr(definition, attribute),
                        f"measurement snapshot {measurement_id}.{attribute}",
                    )
            measurement_snapshot.append(item)

        policy = {
            "policy_version": capture_policy_version,
            "burst_frame_count": FRAME_COUNT,
            "anchor_frame": "F03",
            "burst_interval_target_ms": 150,
            "required_modalities": ["rgb", "depth_raw", "depth_aligned"],
            "optional_modalities": ["ir", "ir_left", "ir_right"],
            "qc_policy_version": "qc-policy-v1.0",
            "warn_requires_manual_review": True,
            # Generic library users can opt out while the production capture
            # profile enables these authoritative-boundary checks.
            "strict_qc_contract": False,
            "require_anthropometry_equipment": False,
            "lock_camera_fingerprint": False,
        }
        if capture_policy is not None:
            supplied_policy = self._json_ready(capture_policy, "capture_policy")
            if not isinstance(supplied_policy, dict):
                raise ProtocolValidationError("capture_policy must be a mapping")
            policy.update(supplied_policy)
        if int(policy.get("burst_frame_count", 0)) != FRAME_COUNT:
            raise ProtocolValidationError(
                f"capture_policy burst_frame_count must be {FRAME_COUNT}"
            )
        if policy.get("anchor_frame") != "F03":
            raise ProtocolValidationError("capture_policy anchor_frame must be F03")

        canonical_conditions = [self._json_ready(item, "protocol conditions") for item in conditions]
        if bool(policy.get("strict_qc_contract", False)):
            condition_ids = [str(item["condition_id"]) for item in canonical_conditions]
            policy_hashes = policy.get("qc_policy_sha256_by_condition")
            if not isinstance(policy_hashes, Mapping) or set(policy_hashes) != set(
                condition_ids
            ):
                raise ProtocolValidationError(
                    "strict capture policy requires qc_policy_sha256_by_condition "
                    "for every frozen condition"
                )
            policy_bodies = policy.get("qc_policy_by_condition")
            if not isinstance(policy_bodies, Mapping) or set(policy_bodies) != set(
                condition_ids
            ):
                raise ProtocolValidationError(
                    "strict capture policy requires qc_policy_by_condition "
                    "for every frozen condition"
                )
            for condition_id, digest in policy_hashes.items():
                if not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
                    raise ProtocolValidationError(
                        f"invalid frozen QC policy sha256 for {condition_id}"
                    )
                body = policy_bodies.get(condition_id)
                if not isinstance(body, Mapping):
                    raise ProtocolValidationError(
                        f"frozen QC policy body must be a mapping for {condition_id}"
                    )
                if self._canonical_sha256(body) != str(digest):
                    raise ProtocolValidationError(
                        f"frozen QC policy body/hash mismatch for {condition_id}"
                    )
            required_counts = policy.get("required_qc_check_counts")
            if not isinstance(required_counts, Mapping):
                raise ProtocolValidationError(
                    "strict capture policy requires required_qc_check_counts"
                )
            normalized_counts: Dict[str, int] = {}
            for code, count in required_counts.items():
                try:
                    normalized_count = int(count)
                except (TypeError, ValueError) as exc:
                    raise ProtocolValidationError(
                        f"invalid required QC check count for {code}"
                    ) from exc
                if normalized_count < 1:
                    raise ProtocolValidationError(
                        f"required QC check count must be positive for {code}"
                    )
                normalized_counts[str(code)] = normalized_count
            missing_contract = {
                code: count
                for code, count in STRICT_QC_REQUIRED_CHECK_COUNTS.items()
                if normalized_counts.get(code) != count
            }
            if missing_contract:
                raise ProtocolValidationError(
                    "strict QC check-count contract is incomplete or wrong: "
                    + ", ".join(
                        f"{code}={count}" for code, count in missing_contract.items()
                    )
                )
            policy["qc_policy_sha256_by_condition"] = {
                condition_id: str(policy_hashes[condition_id])
                for condition_id in condition_ids
            }
            policy["qc_policy_by_condition"] = {
                condition_id: self._json_ready(
                    policy_bodies[condition_id],
                    f"qc_policy_by_condition.{condition_id}",
                )
                for condition_id in condition_ids
            }
            policy["required_qc_check_counts"] = normalized_counts
        payload: Dict[str, Any] = {
            "schema_version": PROTOCOL_SNAPSHOT_SCHEMA_VERSION,
            "protocol_version": protocol_version,
            "profile_id": profile_id,
            "created_at": self._now(),
            "conditions": canonical_conditions,
            "condition_ids": [item["condition_id"] for item in canonical_conditions],
            "measurements": measurement_snapshot,
            "capture_policy": policy,
        }
        if reconstructed_from_subject_state:
            payload["reconstructed_from_subject_state"] = True
        payload["sha256"] = self._canonical_sha256(payload)
        return payload

    def _ensure_protocol_snapshot(self, state: MutableMapping[str, Any]) -> bool:
        """Ensure legacy state has a self-contained, durable snapshot."""

        changed = False
        snapshot = state.get("protocol_snapshot")
        if not isinstance(snapshot, Mapping) or "conditions" not in snapshot:
            conditions = [
                {
                    "order": index,
                    **self._normalize_condition({"condition_id": condition_id}),
                }
                for index, condition_id in enumerate(
                    state.get("expected_condition_ids", []), 1
                )
            ]
            snapshot = self._build_protocol_snapshot(
                protocol_version=state["protocol_version"],
                profile_id=state["profile_id"],
                conditions=conditions,
                capture_policy_version=state.get(
                    "capture_policy_version", CAPTURE_POLICY_VERSION
                ),
                reconstructed_from_subject_state=True,
            )
            state["protocol_snapshot"] = snapshot
            state.setdefault(
                "capture_policy_version",
                snapshot["capture_policy"]["policy_version"],
            )
            changed = True
        snapshot = self._json_ready(snapshot, "protocol_snapshot")
        self._validate_protocol_snapshot(snapshot, state)

        snapshot_file = state.get("protocol_snapshot_file")
        path_text = "meta/protocol_snapshot.json"
        if isinstance(snapshot_file, Mapping) and snapshot_file.get("path"):
            path_text = str(snapshot_file["path"])
        path = self._safe_subject_relative_path(state["subject_id"], path_text)
        rewrite_file = not path.exists()
        if path.exists():
            try:
                expected_file_hash = (
                    snapshot_file.get("sha256")
                    if isinstance(snapshot_file, Mapping)
                    else None
                )
                if expected_file_hash and self._sha256(path) != expected_file_hash:
                    raise ProtocolStoreError("protocol snapshot file hash mismatch")
                on_disk = self._read_json(path)
                self._validate_protocol_snapshot(on_disk, state)
                if on_disk != snapshot:
                    raise ProtocolStoreError(
                        "protocol snapshot file differs from immutable subject state"
                    )
                rewrite_file = False
            except ProtocolStoreError:
                # Do not overwrite a corrupt snapshot.  Preserve it as audit
                # evidence and surface the failure to the caller.
                raise
        if rewrite_file:
            self._atomic_write_json(path, snapshot)
            changed = True
        file_sha = self._sha256(path)
        expected_file = {"path": path_text, "sha256": file_sha}
        if state.get("protocol_snapshot_file") != expected_file:
            state["protocol_snapshot_file"] = expected_file
            changed = True
        return changed

    def _read_protocol_snapshot_file(self, state: Mapping[str, Any]) -> Dict[str, Any]:
        snapshot_file = state.get("protocol_snapshot_file", {})
        path_text = str(snapshot_file.get("path", "meta/protocol_snapshot.json"))
        path = self._safe_subject_relative_path(state["subject_id"], path_text)
        if not path.is_file():
            raise ProtocolStoreError(f"protocol snapshot is missing: {path_text}")
        expected_file_hash = snapshot_file.get("sha256")
        if expected_file_hash and self._sha256(path) != expected_file_hash:
            raise ProtocolStoreError(f"protocol snapshot file hash mismatch: {path_text}")
        snapshot = self._read_json(path)
        self._validate_protocol_snapshot(snapshot, state)
        if state.get("protocol_snapshot") != snapshot:
            raise ProtocolStoreError("protocol snapshot differs from subject state")
        return snapshot

    def _validate_protocol_snapshot(
        self, snapshot: Mapping[str, Any], state: Optional[Mapping[str, Any]] = None
    ) -> None:
        required = {
            "schema_version",
            "protocol_version",
            "profile_id",
            "conditions",
            "condition_ids",
            "measurements",
            "capture_policy",
            "sha256",
        }
        missing = sorted(required - set(snapshot))
        if missing:
            raise ProtocolStoreError(
                "protocol snapshot is missing fields: " + ", ".join(missing)
            )
        unhashed = dict(snapshot)
        supplied_hash = str(unhashed.pop("sha256"))
        actual_hash = self._canonical_sha256(unhashed)
        if supplied_hash != actual_hash:
            raise ProtocolStoreError("protocol snapshot content hash mismatch")
        conditions = snapshot.get("conditions")
        condition_ids = snapshot.get("condition_ids")
        if not isinstance(conditions, list) or not isinstance(condition_ids, list):
            raise ProtocolStoreError("protocol snapshot conditions must be ordered lists")
        derived_ids = [item.get("condition_id") for item in conditions if isinstance(item, Mapping)]
        if len(derived_ids) != len(conditions) or derived_ids != condition_ids:
            raise ProtocolStoreError("protocol snapshot condition order/IDs are inconsistent")
        if len(set(condition_ids)) != len(condition_ids):
            raise ProtocolStoreError("protocol snapshot contains duplicate conditions")
        if state is not None:
            if condition_ids != list(state.get("expected_condition_ids", [])):
                raise ProtocolStoreError(
                    "protocol snapshot condition IDs differ from subject state"
                )
            if snapshot.get("protocol_version") != state.get("protocol_version"):
                raise ProtocolStoreError("protocol snapshot version differs from subject state")
            if snapshot.get("profile_id") != state.get("profile_id"):
                raise ProtocolStoreError("protocol snapshot profile differs from subject state")

    def _resolve_expected_conditions(
        self,
        profile_id: str,
        expected_conditions: Optional[Iterable[Any]],
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        source: Any = expected_conditions
        if source is None and self.protocol is not None:
            source = self._profile_from_provider(self.protocol, profile_id)
        if source is None:
            # The shared protocol package is intentionally imported lazily so
            # this standalone storage module remains usable in isolation.
            try:
                from backend import protocol as protocol_module  # type: ignore

                getter = getattr(protocol_module, "get_profile", None)
                if callable(getter):
                    source = getter(profile_id)
            except (ImportError, KeyError, LookupError, ValueError):
                source = None
        source = self._extract_conditions_from_profile(source)
        if source is None:
            raise ProtocolValidationError(
                "expected_conditions is required when no protocol profile provider is available"
            )
        if isinstance(source, (str, bytes)):
            raise ProtocolValidationError("expected_conditions must be a sequence, not a string")

        condition_ids: List[str] = []
        condition_records: List[Dict[str, Any]] = []
        for item in source:
            if isinstance(item, str):
                normalized = self._normalize_condition({"condition_id": item})
                original_payload: Dict[str, Any] = {}
            else:
                original_payload = self._json_ready(
                    self._as_mapping(item, "expected condition"),
                    "expected condition payload",
                )
                normalized = self._normalize_condition(item)
            condition_id = normalized["condition_id"]
            if condition_id in condition_ids:
                raise ProtocolValidationError(f"duplicate expected condition: {condition_id}")
            condition_ids.append(condition_id)
            condition_records.append(
                {
                    "order": len(condition_records) + 1,
                    **original_payload,
                    **normalized,
                    "distance_mm": normalized["distance_nominal_mm"],
                }
            )
        if not condition_ids:
            raise ProtocolValidationError("expected_conditions cannot be empty")
        return condition_ids, condition_records

    def _profile_from_provider(self, provider: Any, profile_id: str) -> Any:
        if hasattr(provider, "get_profile") and callable(provider.get_profile):
            return provider.get_profile(profile_id)
        if callable(provider):
            return provider(profile_id)
        if isinstance(provider, Mapping):
            if profile_id in provider:
                return provider[profile_id]
            profiles = provider.get("profiles")
            if isinstance(profiles, Mapping) and profile_id in profiles:
                return profiles[profile_id]
        return None

    @staticmethod
    def _extract_conditions_from_profile(profile: Any) -> Any:
        if profile is None:
            return None
        if isinstance(profile, Mapping):
            for key in ("expected_conditions", "conditions", "condition_ids"):
                if key in profile:
                    return profile[key]
            return profile
        for attr in ("expected_conditions", "conditions", "condition_ids"):
            if hasattr(profile, attr):
                return getattr(profile, attr)
        return profile

    # ------------------------------------------------------------------
    # Condition normalization
    # ------------------------------------------------------------------
    def _normalize_condition(self, value: Mapping[str, Any] | Any) -> Dict[str, Any]:
        mapping = self._as_mapping(value, "condition")
        explicit = mapping.get("condition_id") or mapping.get("id")
        parsed: Dict[str, Any] = {}
        if explicit:
            explicit_id = self._validate_condition_id(str(self._enum_value(explicit)))
            match = _CONDITION_ID_RE.fullmatch(explicit_id)
            assert match is not None
            parsed = {
                "camera_code": match.group("camera"),
                "distance_nominal_mm": int(match.group("distance")),
                "view_yaw_deg": int(match.group("view")),
                "light_id": match.group("light"),
                "pose_id": match.group("pose"),
                "clothing_id": match.group("clothing"),
                "repeat_id": int(match.group("repeat")),
            }

        camera = self._camera_code(self._first(mapping, "camera_code", "camera_id", "camera", "camera_model"))
        distance = self._distance(self._first(mapping, "distance_nominal_mm", "distance_mm", "distance"))
        view = self._view(self._first(mapping, "view_yaw_deg", "yaw_deg", "view_yaw", "view"))
        light = self._prefixed_code(self._first(mapping, "light_id", "lighting_id", "light", "lighting"), "L")
        pose = self._prefixed_code(self._first(mapping, "pose_id", "pose"), "P")
        clothing = self._prefixed_code(self._first(mapping, "clothing_id", "clothing"), "C")
        repeat = self._repeat(self._first(mapping, "repeat_id", "repeat"))

        supplied = {
            "camera_code": camera,
            "distance_nominal_mm": distance,
            "view_yaw_deg": view,
            "light_id": light,
            "pose_id": pose,
            "clothing_id": clothing,
            "repeat_id": repeat,
        }
        normalized: Dict[str, Any] = {}
        for key, supplied_value in supplied.items():
            if supplied_value is None:
                if key not in parsed:
                    raise ProtocolValidationError(f"condition is missing {key}")
                normalized[key] = parsed[key]
            else:
                if key in parsed and parsed[key] != supplied_value:
                    raise ProtocolValidationError(
                        f"condition_id conflicts with {key}: {parsed[key]!r} != {supplied_value!r}"
                    )
                normalized[key] = supplied_value

        generated = (
            f"{normalized['camera_code']}_D{normalized['distance_nominal_mm']:04d}_"
            f"V{normalized['view_yaw_deg']:03d}_{normalized['light_id']}_"
            f"{normalized['pose_id']}_{normalized['clothing_id']}_"
            f"R{normalized['repeat_id']:02d}"
        )
        if explicit and generated != explicit_id:
            raise ProtocolValidationError(f"non-canonical condition_id: {explicit_id}")
        normalized["condition_id"] = generated
        # Keep additional protocol metadata, but never allow it to redefine
        # canonical naming fields.
        existing_metadata = mapping.get("metadata")
        if existing_metadata is not None and not isinstance(existing_metadata, Mapping):
            raise ProtocolValidationError("condition.metadata must be a mapping")
        extras = {
            str(key): self._json_ready(item, f"condition.metadata.{key}")
            for key, item in (existing_metadata or {}).items()
        }
        for key, item in mapping.items():
            if key in {
                "id",
                "condition_id",
                "camera_code",
                "camera_id",
                "camera",
                "camera_model",
                "distance_nominal_mm",
                "distance_mm",
                "distance",
                "view_yaw_deg",
                "yaw_deg",
                "view_yaw",
                "view",
                "light_id",
                "lighting_id",
                "light",
                "lighting",
                "pose_id",
                "pose",
                "clothing_id",
                "clothing",
                "repeat_id",
                "repeat",
                "attempt_id",
                "_attempt_id",
                "metadata",
            }:
                continue
            key_text = str(key)
            normalized_item = self._json_ready(item, f"condition.{key_text}")
            if key_text in extras and extras[key_text] != normalized_item:
                raise ProtocolValidationError(
                    f"condition metadata conflicts for {key_text}"
                )
            extras[key_text] = normalized_item
        if extras:
            normalized["metadata"] = extras
        return normalized

    def _assert_condition_compatible(
        self, expected: Mapping[str, Any], supplied: Mapping[str, Any]
    ) -> None:
        keys = (
            "condition_id",
            "camera_code",
            "distance_nominal_mm",
            "view_yaw_deg",
            "light_id",
            "pose_id",
            "clothing_id",
            "repeat_id",
        )
        mismatches = [key for key in keys if expected.get(key) != supplied.get(key)]
        if mismatches:
            raise ProtocolValidationError(
                "condition differs from begun attempt: " + ", ".join(mismatches)
            )

    @staticmethod
    def _extract_attempt_id(condition: Any) -> Optional[str]:
        if isinstance(condition, Mapping):
            value = condition.get("attempt_id") or condition.get("_attempt_id")
            return str(value) if value else None
        for attr in ("attempt_id", "_attempt_id"):
            value = getattr(condition, attr, None)
            if value:
                return str(value)
        return None

    # ------------------------------------------------------------------
    # Frame normalization and durable image writing
    # ------------------------------------------------------------------
    def _normalize_frames(self, frames: Any) -> List[Dict[str, Any]]:
        if isinstance(frames, Mapping) and "frames" in frames:
            frames = frames["frames"]
        elif isinstance(frames, Mapping):
            frames = self._transpose_modality_mapping(frames)
        if isinstance(frames, np.ndarray) or isinstance(frames, (str, bytes, Mapping)):
            raise ProtocolValidationError("frames must be a five-item burst")
        try:
            items = list(frames)
        except TypeError as exc:
            raise ProtocolValidationError("frames must be iterable") from exc
        if len(items) != FRAME_COUNT:
            raise ProtocolValidationError(
                f"a capture attempt requires exactly {FRAME_COUNT} frames; got {len(items)}"
            )

        normalized: List[Dict[str, Any]] = []
        for index, item in enumerate(items, 1):
            mapping = self._as_mapping(item, f"frames[{index - 1}]")
            rgb = self._first(mapping, "rgb", "color", "color_data")
            depth_raw = self._first(mapping, "depth_raw", "raw_depth", "depth_raw_data")
            depth_aligned = self._first(
                mapping,
                "depth_aligned",
                "aligned_depth",
                "depth_to_color",
                "depth",
            )
            if rgb is None or depth_raw is None or depth_aligned is None:
                missing = [
                    name
                    for name, value in (
                        ("rgb", rgb),
                        ("depth_raw", depth_raw),
                        ("depth_aligned", depth_aligned),
                    )
                    if value is None
                ]
                raise ProtocolValidationError(
                    f"frame {index} is missing required modalities: {', '.join(missing)}"
                )

            ir: Dict[str, np.ndarray] = {}
            direct_ir = self._first(mapping, "ir", "infrared")
            left_ir = self._first(mapping, "ir_left", "left_ir", "infrared_left")
            right_ir = self._first(mapping, "ir_right", "right_ir", "infrared_right")
            if isinstance(direct_ir, Mapping):
                left_ir = left_ir if left_ir is not None else self._first(direct_ir, "left", "ir_left")
                right_ir = right_ir if right_ir is not None else self._first(direct_ir, "right", "ir_right")
                direct_ir = self._first(direct_ir, "mono", "single", "ir")
            elif (
                direct_ir is not None
                and not isinstance(direct_ir, np.ndarray)
                and isinstance(direct_ir, Sequence)
                and len(direct_ir) == 2
            ):
                left_ir, right_ir = direct_ir
                direct_ir = None
            if direct_ir is not None:
                ir["ir"] = self._validate_image(direct_ir, f"frame {index} ir", "ir")
            if left_ir is not None:
                ir["ir_left"] = self._validate_image(left_ir, f"frame {index} ir_left", "ir")
            if right_ir is not None:
                ir["ir_right"] = self._validate_image(right_ir, f"frame {index} ir_right", "ir")

            frame_metadata = {}
            for key in (
                "timestamp",
                "timestamp_ns",
                "device_timestamp",
                "frame_number",
                "host_timestamp_ns",
                "stream_timestamps",
                "stream_frame_numbers",
                "frame_camera_metadata",
                "depth_scale",
                "exposure",
                "gain",
                "white_balance",
            ):
                value = mapping.get(key)
                if value is not None:
                    frame_metadata[key] = self._json_ready(value, f"frame {index}.{key}")
            normalized.append(
                {
                    "frame_index": index,
                    "rgb": self._validate_image(rgb, f"frame {index} rgb", "rgb"),
                    "depth_raw": self._validate_image(
                        depth_raw, f"frame {index} depth_raw", "depth"
                    ),
                    "depth_aligned": self._validate_image(
                        depth_aligned, f"frame {index} depth_aligned", "depth"
                    ),
                    "ir": ir,
                    "metadata": frame_metadata,
                }
            )
        return normalized

    def _transpose_modality_mapping(self, frames: Mapping[str, Any]) -> List[Dict[str, Any]]:
        recognized = {
            "rgb",
            "color",
            "depth_raw",
            "raw_depth",
            "depth_aligned",
            "aligned_depth",
            "depth",
            "ir",
            "ir_left",
            "left_ir",
            "ir_right",
            "right_ir",
            "timestamp",
            "timestamp_ns",
            "device_timestamp",
            "frame_number",
            "host_timestamp_ns",
            "stream_timestamps",
            "stream_frame_numbers",
            "frame_camera_metadata",
            "depth_scale",
        }
        if not any(key in frames for key in recognized):
            raise ProtocolValidationError("frames mapping contains no recognized modalities")
        result = [dict() for _ in range(FRAME_COUNT)]
        for key, value in frames.items():
            if key not in recognized:
                continue
            if isinstance(value, np.ndarray):
                if value.ndim < 1 or value.shape[0] != FRAME_COUNT:
                    raise ProtocolValidationError(
                        f"burst modality {key} must have first dimension {FRAME_COUNT}"
                    )
                values = [value[index] for index in range(FRAME_COUNT)]
            else:
                try:
                    values = list(value)
                except TypeError as exc:
                    raise ProtocolValidationError(f"burst modality {key} is not a sequence") from exc
                if len(values) != FRAME_COUNT:
                    raise ProtocolValidationError(
                        f"burst modality {key} requires {FRAME_COUNT} values; got {len(values)}"
                    )
            for index, item in enumerate(values):
                result[index][key] = item
        return result

    def _validate_image(self, value: Any, label: str, modality: str) -> np.ndarray:
        if not isinstance(value, np.ndarray):
            raise ProtocolValidationError(f"{label} must be a numpy array")
        if value.size == 0:
            raise ProtocolValidationError(f"{label} cannot be empty")
        if value.dtype not in (np.dtype(np.uint8), np.dtype(np.uint16)):
            raise ProtocolValidationError(
                f"{label} must use uint8 or uint16 for lossless PNG storage; got {value.dtype}"
            )
        if modality == "rgb":
            if value.ndim != 3 or value.shape[2] not in (3, 4) or value.dtype != np.uint8:
                raise ProtocolValidationError(f"{label} must be uint8 HxWx3 or HxWx4")
        elif modality == "depth":
            if value.ndim != 2 or value.dtype != np.uint16:
                raise ProtocolValidationError(f"{label} must be a uint16 HxW depth image")
        elif modality == "ir" and value.ndim != 2:
            raise ProtocolValidationError(f"{label} must be a 2D IR image")
        return np.ascontiguousarray(value)

    def _validate_burst_consistency(
        self,
        frames: Sequence[Mapping[str, Any]],
        camera_metadata: Mapping[str, Any],
    ) -> None:
        first = frames[0]
        expected_shapes = {
            "rgb": tuple(first["rgb"].shape),
            "depth_raw": tuple(first["depth_raw"].shape),
            "depth_aligned": tuple(first["depth_aligned"].shape),
        }
        expected_ir = set(first["ir"])
        for frame in frames:
            for modality, shape in expected_shapes.items():
                if tuple(frame[modality].shape) != shape:
                    raise ProtocolValidationError(
                        f"burst {modality} shapes are inconsistent"
                    )
            if set(frame["ir"]) != expected_ir:
                raise ProtocolValidationError("IR stream set changes within the burst")
            for ir_name in expected_ir:
                if tuple(frame["ir"][ir_name].shape) != tuple(first["ir"][ir_name].shape):
                    raise ProtocolValidationError(f"burst {ir_name} shapes are inconsistent")
        if expected_shapes["depth_aligned"][:2] != expected_shapes["rgb"][:2]:
            raise ProtocolValidationError(
                "depth_aligned dimensions must match RGB dimensions"
            )

        profile_names = {
            "rgb": ("color", "rgb"),
            "depth_raw": ("depth_raw", "depth"),
            "depth_aligned": ("depth_aligned",),
            "ir_left": ("infrared_left", "ir_left"),
            "ir_right": ("infrared_right", "ir_right"),
            "ir": ("infrared", "ir"),
        }
        arrays = {
            "rgb": first["rgb"],
            "depth_raw": first["depth_raw"],
            "depth_aligned": first["depth_aligned"],
            **first["ir"],
        }
        profiles = camera_metadata.get("stream_profiles")
        if isinstance(profiles, Mapping):
            for modality, array in arrays.items():
                profile = next(
                    (
                        profiles[name]
                        for name in profile_names.get(modality, (modality,))
                        if name in profiles and isinstance(profiles[name], Mapping)
                    ),
                    None,
                )
                if profile is None:
                    continue
                width = profile.get("width")
                height = profile.get("height")
                if width is not None and height is not None and (
                    int(height), int(width)
                ) != tuple(array.shape[:2]):
                    raise ProtocolValidationError(
                        f"{modality} shape {array.shape[:2]} differs from stream profile "
                        f"{(int(height), int(width))}"
                    )
                dtype = profile.get("dtype")
                if dtype is not None and str(dtype).lower() != str(array.dtype).lower():
                    raise ProtocolValidationError(
                        f"{modality} dtype {array.dtype} differs from stream profile {dtype}"
                    )

        observed = camera_metadata.get("observed_streams")
        if isinstance(observed, Mapping):
            for modality, array in arrays.items():
                observation = next(
                    (
                        observed[name]
                        for name in profile_names.get(modality, (modality,))
                        if name in observed and isinstance(observed[name], Mapping)
                    ),
                    None,
                )
                if observation is None:
                    continue
                shape = observation.get("shape")
                if not isinstance(shape, Sequence) or isinstance(shape, (str, bytes)):
                    raise ProtocolValidationError(
                        f"observed_streams.{modality}.shape must be an array"
                    )
                try:
                    observed_shape = tuple(int(value) for value in shape)
                except (TypeError, ValueError) as exc:
                    raise ProtocolValidationError(
                        f"observed_streams.{modality}.shape is invalid"
                    ) from exc
                if observed_shape != tuple(array.shape):
                    raise ProtocolValidationError(
                        f"{modality} shape {tuple(array.shape)} differs from observed_streams "
                        f"{observed_shape}"
                    )
                dtype = observation.get("dtype")
                if dtype is None or str(dtype).lower() != str(array.dtype).lower():
                    raise ProtocolValidationError(
                        f"{modality} dtype {array.dtype} differs from observed_streams {dtype}"
                    )

    def _capture_policy_from_state(
        self, state: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        snapshot = self._read_protocol_snapshot_file(state)
        policy = snapshot.get("capture_policy")
        if not isinstance(policy, Mapping):
            raise ProtocolStoreError("protocol snapshot has no capture_policy")
        return policy

    @staticmethod
    def _camera_serial_from_metadata(camera_metadata: Mapping[str, Any]) -> str:
        device = camera_metadata.get("device")
        device = device if isinstance(device, Mapping) else {}
        for source in (device, camera_metadata):
            for key in (
                "serial_number",
                "camera_serial",
                "serial",
                "uid",
                "device_uid",
                "id",
            ):
                value = source.get(key)
                if value is not None and str(value).strip():
                    return str(value).strip()
        return ""

    @staticmethod
    def _stream_profile_fingerprint(
        camera_metadata: Mapping[str, Any]
    ) -> Dict[str, Dict[str, Any]]:
        raw_profiles = camera_metadata.get("stream_profiles")
        raw_observed = camera_metadata.get("observed_streams")
        profiles = (
            {str(key): value for key, value in raw_profiles.items()}
            if isinstance(raw_profiles, Mapping)
            else {}
        )
        observed = (
            {str(key): value for key, value in raw_observed.items()}
            if isinstance(raw_observed, Mapping)
            else {}
        )
        names = sorted(set(profiles) | set(observed))
        result: Dict[str, Dict[str, Any]] = {}
        stable_keys = (
            "width",
            "height",
            "fps",
            "format",
            "dtype",
            "source",
            "stream_index",
        )
        for name in names:
            profile = profiles.get(name)
            profile = profile if isinstance(profile, Mapping) else {}
            snapshot = {
                key: profile[key]
                for key in stable_keys
                if profile.get(key) is not None
            }
            observation = observed.get(name)
            observation = observation if isinstance(observation, Mapping) else {}
            shape = observation.get("shape")
            if isinstance(shape, Sequence) and not isinstance(shape, (str, bytes)):
                snapshot["observed_shape"] = list(shape)
            if observation.get("dtype") is not None:
                snapshot["observed_dtype"] = observation.get("dtype")
            if snapshot:
                result[name] = snapshot
        return result

    def _camera_fingerprint(
        self,
        condition: Mapping[str, Any],
        camera_metadata: Mapping[str, Any],
    ) -> Tuple[Dict[str, Any], str]:
        serial = self._camera_serial_from_metadata(camera_metadata)
        profiles = self._stream_profile_fingerprint(camera_metadata)
        calibration_sha256 = str(
            camera_metadata.get("calibration_sha256") or ""
        ).strip().lower()
        try:
            depth_scale = float(camera_metadata.get("depth_scale_mm_per_unit"))
        except (TypeError, ValueError):
            depth_scale = 0.0
        missing = []
        if not serial:
            missing.append("camera serial/UID")
        if not profiles:
            missing.append("stream profiles")
        if not re.fullmatch(r"[0-9a-f]{64}", calibration_sha256):
            missing.append("calibration_sha256")
        if not math.isfinite(depth_scale) or depth_scale <= 0:
            missing.append("depth_scale_mm_per_unit")
        if missing:
            raise ProtocolValidationError(
                "camera fingerprint is incomplete: " + ", ".join(missing)
            )
        fingerprint = {
            "schema_version": "1.0",
            "camera_code": str(condition["camera_code"]),
            "camera_serial": serial,
            "stream_profiles": profiles,
            "calibration_sha256": calibration_sha256,
            "depth_scale_mm_per_unit": depth_scale,
        }
        return fingerprint, self._canonical_sha256(fingerprint)

    def _enforce_subject_camera_fingerprint(
        self,
        state: Mapping[str, Any],
        condition: Mapping[str, Any],
        camera_metadata: MutableMapping[str, Any],
    ) -> None:
        policy = self._capture_policy_from_state(state)
        if not bool(policy.get("lock_camera_fingerprint", False)):
            return
        fingerprint, fingerprint_sha256 = self._camera_fingerprint(
            condition,
            camera_metadata,
        )
        camera_code = fingerprint["camera_code"]
        for attempt in state.get("attempts", {}).values():
            if attempt.get("status") != "COMMITTED":
                continue
            prior_condition = attempt.get("condition")
            if (
                not isinstance(prior_condition, Mapping)
                or str(prior_condition.get("camera_code")) != camera_code
            ):
                continue
            prior_metadata = attempt.get("camera_metadata")
            if not isinstance(prior_metadata, Mapping):
                raise ProtocolValidationError(
                    f"existing {camera_code} attempt has no camera metadata"
                )
            prior_fingerprint = prior_metadata.get("subject_camera_fingerprint")
            if isinstance(prior_fingerprint, Mapping):
                prior_fingerprint = dict(prior_fingerprint)
                prior_sha256 = self._canonical_sha256(prior_fingerprint)
            else:
                prior_fingerprint, prior_sha256 = self._camera_fingerprint(
                    prior_condition,
                    prior_metadata,
                )
            if prior_sha256 != fingerprint_sha256:
                changed = sorted(
                    key
                    for key in fingerprint
                    if prior_fingerprint.get(key) != fingerprint.get(key)
                )
                raise ProtocolValidationError(
                    f"subject camera fingerprint changed for {camera_code}: "
                    + ", ".join(changed)
                )
            break
        camera_metadata["subject_camera_fingerprint"] = fingerprint
        camera_metadata["subject_camera_fingerprint_sha256"] = fingerprint_sha256

    def _required_modalities_from_state(
        self, state: Mapping[str, Any]
    ) -> set[str]:
        policy = self._capture_policy_from_state(state)
        raw = policy.get("required_modalities")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise ProtocolStoreError("capture_policy.required_modalities must be a list")
        aliases = {
            "color": "rgb",
            "depth": "depth_raw",
            "infrared": "ir",
            "infrared_left": "ir_left",
            "infrared_right": "ir_right",
        }
        allowed = {"rgb", "depth_raw", "depth_aligned", "ir", "ir_left", "ir_right"}
        normalized = {aliases.get(str(item), str(item)) for item in raw}
        unknown = normalized - allowed
        if unknown:
            raise ProtocolStoreError(
                "capture_policy has unsupported required modalities: "
                + ", ".join(sorted(unknown))
            )
        baseline = {"rgb", "depth_raw", "depth_aligned"}
        if not baseline.issubset(normalized):
            raise ProtocolStoreError(
                "capture_policy cannot omit rgb/depth_raw/depth_aligned"
            )
        return normalized

    @staticmethod
    def _validate_required_modalities(
        frames: Sequence[Mapping[str, Any]], required_modalities: set[str]
    ) -> None:
        for index, frame in enumerate(frames, 1):
            available = {"rgb", "depth_raw", "depth_aligned"}
            infrared = frame.get("ir", {})
            if isinstance(infrared, Mapping):
                available.update(str(name) for name in infrared)
            missing = sorted(required_modalities - available)
            if missing:
                raise ProtocolValidationError(
                    f"F{index:02d} missing frozen-policy modalities: "
                    + ", ".join(missing)
                )

    def _validate_qc_contract(
        self,
        state: Mapping[str, Any],
        condition: Mapping[str, Any],
        qc: Mapping[str, Any],
        camera_metadata: Mapping[str, Any],
    ) -> None:
        policy = self._capture_policy_from_state(state)
        if not bool(policy.get("strict_qc_contract", False)):
            return
        expected_version = str(policy.get("qc_policy_version") or "")
        if not expected_version or str(qc.get("policy_version") or "") != expected_version:
            raise ProtocolValidationError(
                "qc.policy_version differs from frozen capture policy"
            )
        if str(qc.get("condition_id") or "") != str(condition["condition_id"]):
            raise ProtocolValidationError("qc.condition_id differs from capture condition")
        policy_snapshot = qc.get("policy_snapshot")
        if not isinstance(policy_snapshot, Mapping):
            raise ProtocolValidationError("strict QC requires policy_snapshot")
        supplied_policy_hash = str(qc.get("policy_sha256") or "")
        if supplied_policy_hash != self._canonical_sha256(policy_snapshot):
            raise ProtocolValidationError("qc.policy_sha256 is missing or invalid")
        frozen_policy_hashes = policy.get("qc_policy_sha256_by_condition")
        expected_policy_hash = (
            frozen_policy_hashes.get(condition["condition_id"])
            if isinstance(frozen_policy_hashes, Mapping)
            else None
        )
        if not expected_policy_hash or supplied_policy_hash != expected_policy_hash:
            raise ProtocolValidationError(
                "QC policy hash differs from the condition-specific frozen hash"
            )
        frozen_policy_bodies = policy.get("qc_policy_by_condition")
        expected_policy_body = (
            frozen_policy_bodies.get(condition["condition_id"])
            if isinstance(frozen_policy_bodies, Mapping)
            else None
        )
        if not isinstance(expected_policy_body, Mapping) or policy_snapshot != expected_policy_body:
            raise ProtocolValidationError(
                "QC policy snapshot differs from the condition-specific frozen body"
            )
        if str(policy_snapshot.get("policy_version") or "") != expected_version:
            raise ProtocolValidationError("QC policy snapshot version mismatch")
        if int(policy_snapshot.get("required_frame_count", -1)) != FRAME_COUNT:
            raise ProtocolValidationError("QC policy snapshot frame count mismatch")
        if str(policy_snapshot.get("anchor_frame") or "") != "F03":
            raise ProtocolValidationError("QC policy snapshot anchor frame mismatch")
        snapshot_modalities = policy_snapshot.get("required_modalities")
        if not isinstance(snapshot_modalities, Sequence) or isinstance(
            snapshot_modalities, (str, bytes)
        ):
            raise ProtocolValidationError("QC policy snapshot modalities are invalid")
        if set(map(str, snapshot_modalities)) != self._required_modalities_from_state(state):
            raise ProtocolValidationError(
                "QC policy snapshot modalities differ from frozen capture policy"
            )

        checks = qc.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ProtocolValidationError("strict QC requires non-empty checks")
        statuses: List[str] = []
        codes: List[str] = []
        identities: List[Tuple[str, str]] = []
        for item in checks:
            if not isinstance(item, Mapping):
                raise ProtocolValidationError("QC checks must be objects")
            code = str(item.get("code") or "")
            status = str(item.get("status") or "").upper()
            if not code or status not in {"PASS", "WARN", "FAIL"}:
                raise ProtocolValidationError("QC check code/status is invalid")
            codes.append(code)
            statuses.append(status)
            identities.append((code, str(item.get("frame") or "")))
        if len(identities) != len(set(identities)):
            raise ProtocolValidationError("QC (code, frame) identities must be unique")
        required_counts = policy.get("required_qc_check_counts")
        if not isinstance(required_counts, Mapping):
            raise ProtocolValidationError(
                "frozen strict policy has no required_qc_check_counts"
            )
        expected_frames = {f"F{index:02d}" for index in range(1, FRAME_COUNT + 1)}
        for code, expected_count_raw in required_counts.items():
            expected_count = int(expected_count_raw)
            matching = [item for item in checks if item.get("code") == code]
            if len(matching) != expected_count:
                raise ProtocolValidationError(
                    f"QC check {code} count differs from frozen contract: "
                    f"{len(matching)} != {expected_count}"
                )
            if expected_count == FRAME_COUNT:
                actual_frames = {str(item.get("frame") or "") for item in matching}
                if actual_frames != expected_frames:
                    raise ProtocolValidationError(
                        f"QC check {code} must cover F01–F05 exactly"
                    )
        derived_status = (
            "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "PASS"
        )
        if self._quality_status(qc) != derived_status:
            raise ProtocolValidationError("QC overall status differs from check results")
        if bool(policy.get("warn_requires_manual_review", True)):
            manual_checks = [
                item
                for item in checks
                if item.get("code") == "HUMAN_CONTENT_MANUAL_REVIEW"
            ]
            if len(manual_checks) != 1 or str(
                manual_checks[0].get("status")
            ).upper() != "WARN":
                raise ProtocolValidationError(
                    "frozen policy requires HUMAN_CONTENT_MANUAL_REVIEW as WARN"
                )
            if qc.get("manual_review_required") != (derived_status == "WARN"):
                raise ProtocolValidationError(
                    "qc.manual_review_required differs from QC status"
                )
        if str(camera_metadata.get("qc_policy_version") or "") != expected_version:
            raise ProtocolValidationError("camera metadata QC policy version mismatch")
        if str(camera_metadata.get("qc_policy_sha256") or "") != supplied_policy_hash:
            raise ProtocolValidationError("camera metadata QC policy hash mismatch")

    def _write_burst(
        self,
        staging_dir: Path,
        final_dir: Path,
        subject_id: str,
        condition: Mapping[str, Any],
        frames: Sequence[Mapping[str, Any]],
        camera_metadata: Mapping[str, Any],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        file_records: List[Dict[str, Any]] = []
        frame_records: List[Dict[str, Any]] = []
        color_order = str(camera_metadata.get("rgb_color_order", "RGB")).upper()
        for frame in frames:
            frame_index = int(frame["frame_index"])
            frame_code = f"F{frame_index:02d}"
            basename = f"{subject_id}_{condition['condition_id']}_{frame_code}"
            depth_scale_mm_per_unit = float(
                frame.get("metadata", {}).get(
                    "depth_scale",
                    camera_metadata.get("depth_scale_mm_per_unit", 1.0),
                )
            )
            if depth_scale_mm_per_unit <= 0:
                raise ProtocolValidationError("depth scale must be positive for NPY storage")
            modalities: List[Tuple[str, str, np.ndarray]] = [
                ("rgb", "rgb", frame["rgb"]),
                ("depth_raw", "depth_raw", frame["depth_raw"]),
                ("depth_aligned", "depth_aligned", frame["depth_aligned"]),
            ]
            for ir_name, ir_array in frame["ir"].items():
                modalities.append((ir_name, ir_name, ir_array))

            frame_file_indexes: List[int] = []
            for modality_token, directory_name, image in modalities:
                modality = modality_token
                prepared = image
                if modality == "rgb":
                    if color_order == "RGB":
                        prepared = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
                    elif color_order == "RGBA":
                        prepared = cv2.cvtColor(image, cv2.COLOR_RGBA2BGRA)
                    elif color_order not in {"BGR", "BGRA"}:
                        raise ProtocolValidationError(
                            "camera_metadata.rgb_color_order must be RGB, RGBA, BGR, or BGRA"
                        )
                filename = f"{basename}.png"
                stage_path = staging_dir / directory_name / filename
                self._write_png_checked(stage_path, prepared)
                relative_inside_attempt = stage_path.relative_to(staging_dir)
                final_path = final_dir / relative_inside_attempt
                dataset_relative = final_path.relative_to(self.base_dir)
                record = {
                    "modality": modality,
                    "frame_index": frame_index,
                    "path": dataset_relative.as_posix(),
                    "size_bytes": stage_path.stat().st_size,
                    "sha256": self._sha256(stage_path),
                }
                if modality == "rgb":
                    record.update(
                        {
                            "array_channel_order": color_order,
                            "file_color_space": "sRGB",
                            "png_decoder_channel_order": "implementation_defined",
                        }
                    )
                frame_file_indexes.append(len(file_records))
                file_records.append(record)
                if modality in {"depth_raw", "depth_aligned"}:
                    npy_filename = f"{basename}.npy"
                    npy_stage_path = staging_dir / f"{directory_name}_npy" / npy_filename
                    npy_integrity = atomic_write_npy(npy_stage_path, image)
                    npy_relative_inside_attempt = npy_stage_path.relative_to(staging_dir)
                    npy_final_path = final_dir / npy_relative_inside_attempt
                    npy_dataset_relative = npy_final_path.relative_to(self.base_dir)
                    npy_record = {
                        "modality": f"{modality}_npy",
                        "logical_modality": modality,
                        "frame_index": frame_index,
                        "path": npy_dataset_relative.as_posix(),
                        **npy_integrity,
                        "depth_scale_mm_per_unit": depth_scale_mm_per_unit,
                        "value_semantics": "sensor_depth_units",
                    }
                    frame_file_indexes.append(len(file_records))
                    file_records.append(npy_record)
            frame_records.append(
                {
                    "frame_index": frame_index,
                    "frame_code": frame_code,
                    "is_anchor": frame_index == 3,
                    "metadata": frame["metadata"],
                    "file_indexes": frame_file_indexes,
                }
            )
        return file_records, frame_records

    def _write_png_checked(self, destination: Path, image: np.ndarray) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ProtocolStoreError(f"refusing to overwrite image: {destination}")
        temporary = destination.with_name(
            f".{destination.stem}.{uuid.uuid4().hex}.tmp{destination.suffix}"
        )
        try:
            # imencode + Python file I/O is Unicode-safe on Windows, unlike
            # cv2.imwrite/imread in several OpenCV builds.  The OpenCV success
            # flag, durable byte write, and decode round-trip are all checked.
            ok, encoded = cv2.imencode(
                ".png", image, [cv2.IMWRITE_PNG_COMPRESSION, 0]
            )
            if not ok or encoded is None or encoded.size == 0:
                raise OSError(f"cv2 PNG encoding failed for {destination.name}")
            with open(temporary, "xb") as handle:
                handle.write(encoded.tobytes())
                handle.flush()
                os.fsync(handle.fileno())
            if temporary.stat().st_size == 0:
                raise OSError(f"PNG byte write failed for {destination.name}")
            encoded_check = np.frombuffer(temporary.read_bytes(), dtype=np.uint8)
            decoded = cv2.imdecode(encoded_check, cv2.IMREAD_UNCHANGED)
            if (
                decoded is None
                or decoded.shape != image.shape
                or decoded.dtype != image.dtype
                or not np.array_equal(decoded, image)
            ):
                raise OSError(f"PNG verification failed for {destination.name}")
            self._replace_path_with_windows_retry(
                temporary,
                destination,
                allow_existing_destination=True,
            )
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Anthropometry validation
    # ------------------------------------------------------------------
    @staticmethod
    def _default_measurement_definitions() -> Dict[str, Dict[str, Any]]:
        definitions = {
            measurement_id: {
                **definition,
                "fields": tuple(definition["fields"]),
            }
            for measurement_id, definition in _MEASUREMENT_DEFINITIONS.items()
        }
        for measurement_id, definition in definitions.items():
            if measurement_id == "M01":
                valid_range = ANTHROPOMETRY_HARD_RANGES["height"]
            elif measurement_id == "M02":
                valid_range = ANTHROPOMETRY_HARD_RANGES["weight"]
            elif measurement_id == "M03":
                valid_range = ANTHROPOMETRY_HARD_RANGES["breadth"]
            else:
                valid_range = ANTHROPOMETRY_HARD_RANGES[
                    "length_or_circumference"
                ]
            definition["valid_range"] = tuple(valid_range)
            definition["warning_threshold"] = (
                0.5 if measurement_id == "M02" else None
            )
        return definitions

    def _measurement_definitions_from_state(
        self, state: Mapping[str, Any]
    ) -> Dict[str, Dict[str, Any]]:
        policy = self._capture_policy_from_state(state)
        if not bool(policy.get("strict_qc_contract", False)):
            return self._default_measurement_definitions()
        snapshot = self._read_protocol_snapshot_file(state)
        raw_measurements = snapshot.get("measurements")
        if not isinstance(raw_measurements, list):
            raise ProtocolStoreError("protocol snapshot measurements must be a list")
        definitions: Dict[str, Dict[str, Any]] = {}
        for item in raw_measurements:
            if not isinstance(item, Mapping):
                raise ProtocolStoreError("measurement snapshot entries must be objects")
            measurement_id = str(item.get("measurement_id") or "")
            fields = item.get("field_names")
            valid_range = item.get("valid_range", item.get("hard_range"))
            if (
                measurement_id in definitions
                or measurement_id not in set(ALL_MEASUREMENT_IDS)
                or not isinstance(fields, list)
                or not fields
                or len(fields) != len(set(map(str, fields)))
                or not isinstance(valid_range, Sequence)
                or isinstance(valid_range, (str, bytes))
                or len(valid_range) != 2
            ):
                raise ProtocolStoreError(
                    f"invalid frozen measurement definition: {measurement_id}"
                )
            try:
                lower, upper = (float(valid_range[0]), float(valid_range[1]))
            except (TypeError, ValueError) as exc:
                raise ProtocolStoreError(
                    f"invalid frozen measurement range: {measurement_id}"
                ) from exc
            if not math.isfinite(lower) or not math.isfinite(upper) or lower >= upper:
                raise ProtocolStoreError(
                    f"invalid frozen measurement range: {measurement_id}"
                )
            threshold = item.get("third_measurement_threshold")
            if threshold is not None:
                try:
                    threshold = float(threshold)
                except (TypeError, ValueError) as exc:
                    raise ProtocolStoreError(
                        f"invalid frozen repeat threshold: {measurement_id}"
                    ) from exc
                if not math.isfinite(threshold) or threshold < 0:
                    raise ProtocolStoreError(
                        f"invalid frozen repeat threshold: {measurement_id}"
                    )
            definitions[measurement_id] = {
                "fields": tuple(map(str, fields)),
                "unit": str(item.get("unit") or ""),
                "kind": item.get("kind"),
                "threshold": threshold,
                "required": bool(item.get("required")),
                "valid_range": (lower, upper),
                "warning_threshold": item.get("repeat_warning_threshold"),
            }
        expected_ids = set(ALL_MEASUREMENT_IDS)
        if set(definitions) != expected_ids:
            raise ProtocolStoreError(
                "strict protocol snapshot must freeze M01–M23 definitions"
            )
        required_ids = {
            measurement_id
            for measurement_id, definition in definitions.items()
            if definition["required"]
        }
        supported_required_sets = {
            frozenset(CORE_MEASUREMENT_IDS),
            frozenset(LEGACY_CORE_MEASUREMENT_IDS),
        }
        if frozenset(required_ids) not in supported_required_sets:
            raise ProtocolStoreError(
                "strict snapshot must require the current five measurements "
                "or the frozen legacy M01–M13 set"
            )
        return definitions

    def _validate_anthropometry_metadata(
        self,
        state: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> str:
        operator_id = str(
            metadata.get("operator_id")
            or state.get("subject_metadata", {}).get("operator_id")
            or ""
        ).strip()
        policy = self._capture_policy_from_state(state)
        require_equipment = bool(policy.get("require_anthropometry_equipment", False))
        if require_equipment and not _SAFE_ID_RE.fullmatch(operator_id):
            raise ProtocolValidationError(
                "strict anthropometry requires a valid operator_id"
            )
        equipment = metadata.get("equipment")
        if not require_equipment:
            return operator_id
        if not isinstance(equipment, Mapping):
            raise ProtocolValidationError(
                "strict anthropometry requires metadata.equipment"
            )
        for field_name in REQUIRED_ANTHROPOMETRY_EQUIPMENT_FIELDS:
            value = str(equipment.get(field_name) or "").strip()
            if not _SAFE_ID_RE.fullmatch(value):
                raise ProtocolValidationError(
                    f"metadata.equipment.{field_name} must be a valid equipment ID"
                )
        if equipment.get("equipment_check_confirmed") is not True:
            raise ProtocolValidationError(
                "metadata.equipment.equipment_check_confirmed must be true"
            )
        return operator_id

    def _normalize_anthropometry(
        self,
        measurements: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        definitions: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Normalize both API records and legacy measurement mappings.

        The frontend sends one record per concrete field.  That distinction is
        essential for bilateral definitions: M16, for example, is two
        independent repeat series rather than one ambiguous "upper arm" value.
        """

        measurement_definitions = (
            dict(definitions) if definitions is not None else self._default_measurement_definitions()
        )
        ordered_measurement_ids = tuple(
            measurement_id
            for measurement_id in ALL_MEASUREMENT_IDS
            if measurement_id in measurement_definitions
        )
        required_measurement_ids = tuple(
            measurement_id
            for measurement_id in ordered_measurement_ids
            if bool(measurement_definitions[measurement_id].get("required"))
        )
        optional_measurement_ids = tuple(
            measurement_id
            for measurement_id in ordered_measurement_ids
            if measurement_id not in required_measurement_ids
        )
        field_to_measurement_id = {
            str(field_name).lower(): measurement_id
            for measurement_id, definition in measurement_definitions.items()
            for field_name in definition["fields"]
        }
        field_inputs: Dict[Tuple[str, str], Any] = {}

        def add(measurement_id: str, field_name: str, raw_value: Any) -> None:
            measurement_id = measurement_id.strip().upper()
            if measurement_id not in measurement_definitions:
                raise ProtocolValidationError(f"unknown measurement_id: {measurement_id}")
            definition = measurement_definitions[measurement_id]
            if field_name not in definition["fields"]:
                raise ProtocolValidationError(
                    f"{field_name} is not a field of {measurement_id}; "
                    f"expected {', '.join(definition['fields'])}"
                )
            key = (measurement_id, field_name)
            if key in field_inputs:
                raise ProtocolValidationError(
                    f"duplicate measurement field: {measurement_id}::{field_name}"
                )
            if not self._is_blank(raw_value):
                field_inputs[key] = raw_value

        if isinstance(measurements, Mapping):
            for raw_key, raw_value in measurements.items():
                key = str(raw_key).strip()
                if "::" in key:
                    measurement_id, field_name = key.split("::", 1)
                    add(measurement_id, field_name, raw_value)
                    continue
                upper = key.upper()
                match = re.match(r"^(M\d{2})(?:\b|[_\s-])", upper)
                if match and match.group(1) in measurement_definitions:
                    measurement_id = match.group(1)
                    definition = measurement_definitions[measurement_id]
                    fields = definition["fields"]
                    if self._is_blank(raw_value):
                        continue
                    if len(fields) == 1:
                        field_name = fields[0]
                        if (
                            isinstance(raw_value, Mapping)
                            and field_name in raw_value
                            and not any(
                                read_key in raw_value
                                for read_key in (
                                    "values",
                                    "m1",
                                    "m2",
                                    "m3",
                                    "measurement_1",
                                    "measurement_2",
                                    "measurement_3",
                                )
                            )
                        ):
                            add(measurement_id, field_name, raw_value[field_name])
                        else:
                            add(measurement_id, field_name, raw_value)
                    else:
                        if not isinstance(raw_value, Mapping):
                            raise ProtocolValidationError(
                                f"{measurement_id} must provide each bilateral field separately"
                            )
                        unknown_fields = set(raw_value) - set(fields)
                        if unknown_fields:
                            raise ProtocolValidationError(
                                f"unknown fields for {measurement_id}: "
                                + ", ".join(sorted(map(str, unknown_fields)))
                            )
                        for field_name in fields:
                            if field_name in raw_value:
                                add(measurement_id, field_name, raw_value[field_name])
                    continue
                measurement_id = field_to_measurement_id.get(key.lower())
                if not measurement_id:
                    raise ProtocolValidationError(f"unknown measurement field: {key}")
                canonical_field = next(
                    field
                    for field in measurement_definitions[measurement_id]["fields"]
                    if field.lower() == key.lower()
                )
                add(measurement_id, canonical_field, raw_value)
        else:
            for index, raw_record in enumerate(measurements):
                if not isinstance(raw_record, Mapping):
                    raise ProtocolValidationError(
                        f"measurement record {index} must be a mapping"
                    )
                measurement_id = str(raw_record.get("measurement_id", "")).strip().upper()
                if measurement_id not in measurement_definitions:
                    raise ProtocolValidationError(
                        f"measurement record {index} has invalid measurement_id"
                    )
                fields = measurement_definitions[measurement_id]["fields"]
                field_name = str(raw_record.get("field_name") or "").strip()
                if not field_name and len(fields) == 1:
                    field_name = fields[0]
                if not field_name:
                    raise ProtocolValidationError(
                        f"measurement record {index} requires field_name"
                    )
                add(measurement_id, field_name, raw_record)

        missing_required: List[str] = []
        for measurement_id in required_measurement_ids:
            for field_name in measurement_definitions[measurement_id]["fields"]:
                if (measurement_id, field_name) not in field_inputs:
                    missing_required.append(f"{measurement_id}::{field_name}")
        if missing_required:
            raise ProtocolValidationError(
                "missing required anthropometry fields: " + ", ".join(missing_required)
            )

        # Optional measurements may be completely absent, but bilateral items
        # cannot be half-filled because that would silently bias left/right GT.
        for measurement_id in optional_measurement_ids:
            fields = measurement_definitions[measurement_id]["fields"]
            present = [field for field in fields if (measurement_id, field) in field_inputs]
            if present and len(present) != len(fields):
                missing = [field for field in fields if field not in present]
                raise ProtocolValidationError(
                    f"{measurement_id} is partially filled; missing: {', '.join(missing)}"
                )

        normalized: Dict[str, Any] = {}
        records: List[Dict[str, Any]] = []
        for measurement_id in ordered_measurement_ids:
            definition = measurement_definitions[measurement_id]
            fields = definition["fields"]
            field_results: Dict[str, Dict[str, Any]] = {}
            for field_name in fields:
                raw_value = field_inputs.get((measurement_id, field_name))
                if raw_value is None:
                    continue
                result = self._normalize_measurement_field(
                    measurement_id,
                    field_name,
                    raw_value,
                    definitions=measurement_definitions,
                )
                field_results[field_name] = result
                records.append(
                    {
                        "measurement_id": measurement_id,
                        "field_name": field_name,
                        "m1": result["measurement_1"],
                        "m2": result["measurement_2"],
                        **(
                            {"m3": result["measurement_3"]}
                            if result["measurement_3"] is not None
                            else {}
                        ),
                        "final_value": result["final_value"],
                        "unit": result["unit"],
                        "warnings": list(result.get("warnings", [])),
                    }
                )
            if not field_results:
                normalized[measurement_id] = None
            elif len(fields) == 1:
                normalized[measurement_id] = field_results[fields[0]]
            else:
                normalized[measurement_id] = field_results
        return normalized, records

    def _normalize_measurement_field(
        self,
        measurement_id: str,
        field_name: str,
        value: Any,
        *,
        definitions: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        measurement_definitions = (
            definitions if definitions is not None else self._default_measurement_definitions()
        )
        definition = measurement_definitions[measurement_id]
        if isinstance(value, Mapping):
            if "values" in value:
                values = list(value["values"])
            else:
                values = []
                for long_key, short_key in (
                    ("measurement_1", "m1"),
                    ("measurement_2", "m2"),
                    ("measurement_3", "m3"),
                ):
                    reading = value.get(long_key, value.get(short_key))
                    if not self._is_blank(reading):
                        values.append(reading)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            values = list(value)
        else:
            raise ProtocolValidationError(
                f"{measurement_id}::{field_name} must contain m1/m2[/m3] or a numeric list"
            )
        if len(values) < 2 or len(values) > 3:
            raise ProtocolValidationError(
                f"{measurement_id}::{field_name} requires two or three measurements"
            )
        label = f"{measurement_id}::{field_name}"
        numeric = [self._positive_finite_number(item, label) for item in values]
        lower, upper = definition["valid_range"]
        outside = [item for item in numeric if not lower <= item <= upper]
        if outside:
            raise ProtocolValidationError(
                f"{label} is outside the hard range {lower:g}–{upper:g} {definition['unit']}"
            )
        threshold = definition["threshold"]
        difference = abs(numeric[0] - numeric[1])
        third_required = threshold is not None and difference > threshold
        if third_required and len(numeric) < 3:
            raise ProtocolValidationError(
                f"{label} requires measurement_3: first-two difference "
                f"{difference:g} exceeds {threshold:g} {definition['unit']}"
            )
        if len(numeric) == 2:
            pair = (0, 1)
        else:
            pairs = ((0, 1), (0, 2), (1, 2))
            pair = min(
                pairs,
                key=lambda indexes: (
                    abs(numeric[indexes[0]] - numeric[indexes[1]]),
                    indexes,
                ),
            )
        final_value = (numeric[pair[0]] + numeric[pair[1]]) / 2.0
        warnings = []
        warning_threshold = definition.get("warning_threshold")
        if warning_threshold is not None and difference > float(warning_threshold):
            warnings.append(
                f"{measurement_id} first-two difference {difference:g} "
                f"{definition['unit']} exceeds {float(warning_threshold):g} "
                f"{definition['unit']}; "
                "protocol records a warning without requiring measurement_3"
            )
        return {
            "measurement_id": measurement_id,
            "field_name": field_name,
            "unit": definition["unit"],
            "measurement_1": numeric[0],
            "measurement_2": numeric[1],
            "measurement_3": numeric[2] if len(numeric) == 3 else None,
            "first_two_difference": difference,
            "third_measurement_threshold": threshold,
            "third_measurement_required": third_required,
            "final_value": final_value,
            "final_source_measurements": [pair[0] + 1, pair[1] + 1],
            "warnings": warnings,
        }

    # ------------------------------------------------------------------
    # Completion, state and manifest helpers
    # ------------------------------------------------------------------
    def _attempt_final_dir(
        self,
        subject_id: str,
        condition: Mapping[str, Any],
        attempt_id: str,
    ) -> Path:
        return (
            self._subject_dir(subject_id)
            / "cameras"
            / self._camera_slug(str(condition["camera_code"]))
            / "conditions"
            / str(condition["condition_id"])
            / "attempts"
            / attempt_id
        )

    @staticmethod
    def _promote_attempt_directory(source: Path, destination: Path) -> None:
        """Atomically promote one complete staging tree.

        Windows virus scanners and filesystem indexers can briefly hold a
        newly written PNG or sidecar open, causing ``os.replace`` on its parent
        directory to raise ``PermissionError: [WinError 5]``.  Retrying the
        same atomic rename is safe while the source still exists and the
        destination does not.  No copy/delete fallback is used because that
        would weaken the commit boundary.
        """

        ProtocolStore._replace_path_with_windows_retry(
            source,
            destination,
            allow_existing_destination=False,
        )

    @staticmethod
    def _replace_path_with_windows_retry(
        source: Path,
        destination: Path,
        *,
        allow_existing_destination: bool,
    ) -> None:
        """Run one atomic replace with bounded retries for Windows locks."""

        for attempt_index in range(
            len(_WINDOWS_ATOMIC_REPLACE_RETRY_DELAYS_SEC) + 1
        ):
            if not source.exists():
                raise ProtocolStoreError(
                    f"atomic replace source is missing: {source}"
                )
            if not allow_existing_destination and destination.exists():
                raise ProtocolStoreError(
                    f"attempt destination already exists: {destination}"
                )
            try:
                os.replace(source, destination)
                return
            except OSError as exc:
                winerror = getattr(exc, "winerror", None)
                can_retry = (
                    os.name == "nt"
                    and winerror in _WINDOWS_TRANSIENT_REPLACE_LOCK_ERRORS
                    and attempt_index
                    < len(_WINDOWS_ATOMIC_REPLACE_RETRY_DELAYS_SEC)
                    and source.exists()
                    and (allow_existing_destination or not destination.exists())
                )
                if not can_retry:
                    raise
                time.sleep(
                    _WINDOWS_ATOMIC_REPLACE_RETRY_DELAYS_SEC[attempt_index]
                )

    def _verify_attempt_directory(
        self,
        directory: Path,
        *,
        expected_attempt_id: Optional[str] = None,
        expected_condition_id: Optional[str] = None,
        staging: bool,
        subject_state: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        capture_path = directory / "capture.json"
        if not capture_path.is_file():
            legacy = directory / "attempt.json"
            capture_path = legacy if legacy.is_file() else capture_path
        commit_path = directory / COMMIT_RECORD_FILENAME
        qc_path = directory / "qc.json"
        if not capture_path.is_file() or not qc_path.is_file() or not commit_path.is_file():
            raise ProtocolStoreError(
                "durable capture.json/qc.json/commit.json sidecar set is incomplete"
            )
        capture = self._read_json(capture_path)
        qc = self._read_json(qc_path)
        commit = self._read_json(commit_path)
        attempt_id = str(commit.get("attempt_id") or capture.get("attempt_id") or "")
        condition_id = str(commit.get("condition_id") or capture.get("condition_id") or "")
        if not attempt_id or attempt_id != directory.name:
            raise ProtocolStoreError("attempt directory/commit attempt_id mismatch")
        if expected_attempt_id and attempt_id != expected_attempt_id:
            raise ProtocolStoreError("unexpected attempt_id in durable commit")
        if expected_condition_id and condition_id != expected_condition_id:
            raise ProtocolStoreError("unexpected condition_id in durable commit")
        if capture.get("attempt_id") != attempt_id or capture.get("condition_id") != condition_id:
            raise ProtocolStoreError("capture.json identity differs from commit.json")
        if capture.get("status") != "COMMITTED" or commit.get("status") != "COMMITTED":
            raise ProtocolStoreError("durable commit is not COMMITTED")
        quality = str(capture.get("quality_status", "")).upper()
        if quality not in {"PASS", "WARN", "FAIL"}:
            raise ProtocolStoreError("durable capture has invalid quality_status")
        if commit.get("quality_status") != quality:
            raise ProtocolStoreError("capture.json and commit.json quality differ")
        if capture.get("qc") != qc:
            raise ProtocolStoreError("qc.json differs from capture.json qc payload")
        try:
            qc_quality = self._quality_status(qc)
        except ProtocolValidationError as exc:
            raise ProtocolStoreError(f"qc.json is invalid: {exc}") from exc
        if qc_quality != quality:
            raise ProtocolStoreError("qc.json status differs from durable quality_status")
        condition = capture.get("condition")
        if not isinstance(condition, Mapping):
            raise ProtocolStoreError("capture.json has no canonical condition")
        normalized_condition = self._normalize_condition(condition)
        if normalized_condition["condition_id"] != condition_id:
            raise ProtocolStoreError("capture condition does not match condition_id")
        capture["condition"] = normalized_condition
        subject_from_path = self._subject_id_from_attempt_path(directory, staging)
        stored_subject = str(capture.get("subject_id") or "")
        if stored_subject and stored_subject != subject_from_path:
            raise ProtocolStoreError("capture.json subject_id mismatch")
        capture["subject_id"] = subject_from_path
        if subject_state is not None:
            if subject_state.get("subject_id") != subject_from_path:
                raise ProtocolStoreError("subject state identity differs from attempt path")
            camera_metadata = capture.get("camera_metadata")
            if not isinstance(camera_metadata, Mapping):
                raise ProtocolStoreError("capture.json camera_metadata must be a mapping")
            try:
                self._validate_qc_contract(
                    subject_state,
                    normalized_condition,
                    qc,
                    camera_metadata,
                )
            except ProtocolValidationError as exc:
                raise ProtocolStoreError(f"durable QC contract failed: {exc}") from exc

        files = capture.get("files")
        commit_files = commit.get("files")
        if not isinstance(files, list) or not files:
            raise ProtocolStoreError("capture has no file records")
        if commit_files != files or int(commit.get("file_count", -1)) != len(files):
            raise ProtocolStoreError("commit.json file inventory differs from capture.json")
        embedded_sidecars = commit.get("sidecars")
        if not isinstance(embedded_sidecars, Mapping):
            raise ProtocolStoreError("commit.json has no sidecar integrity inventory")
        for name, path in (("capture.json", capture_path), ("qc.json", qc_path)):
            expected = embedded_sidecars.get(name)
            if not isinstance(expected, Mapping):
                raise ProtocolStoreError(f"commit.json sidecar inventory lacks {name}")
            self._verify_file_integrity_record(path, expected, label=name)
        if not isinstance(capture.get("frames"), list) or len(capture["frames"]) != FRAME_COUNT:
            raise ProtocolStoreError("capture does not contain five frame records")

        final_dir = self._attempt_final_dir(
            subject_from_path,
            normalized_condition,
            attempt_id,
        )
        modalities = {index: set() for index in range(1, FRAME_COUNT + 1)}
        dataset_root = self.base_dir.resolve()
        for record in files:
            if not isinstance(record, Mapping):
                raise ProtocolStoreError("malformed durable file record")
            try:
                frame_index = int(record["frame_index"])
                modality = str(record["modality"])
                dataset_path = (self.base_dir / Path(str(record["path"]))).resolve()
                dataset_path.relative_to(dataset_root)
                relative_inside = dataset_path.relative_to(final_dir.resolve())
            except Exception as exc:
                raise ProtocolStoreError(f"invalid durable file path: {exc}") from exc
            actual_path = directory / relative_inside if staging else dataset_path
            if not actual_path.is_file():
                raise ProtocolStoreError(f"durable file is missing: {record['path']}")
            if actual_path.stat().st_size != int(record.get("size_bytes", -1)):
                raise ProtocolStoreError(f"durable file size mismatch: {record['path']}")
            if self._sha256(actual_path) != str(record.get("sha256", "")):
                raise ProtocolStoreError(f"durable file hash mismatch: {record['path']}")
            if frame_index not in modalities:
                raise ProtocolStoreError(f"invalid frame index: {frame_index}")
            modalities[frame_index].add(modality)
        required_modalities = (
            self._required_modalities_from_state(subject_state)
            if subject_state is not None
            else {"rgb", "depth_raw", "depth_aligned"}
        )
        for frame_index, frame_modalities in modalities.items():
            missing = required_modalities - frame_modalities
            if missing:
                raise ProtocolStoreError(
                    f"F{frame_index:02d} missing modalities: {', '.join(sorted(missing))}"
                )
        capture.setdefault(
            "review_status",
            "NOT_REQUIRED"
            if quality == "PASS"
            else "PENDING"
            if quality == "WARN"
            else "NOT_APPLICABLE",
        )
        capture.setdefault("review", None)
        capture.setdefault("validity", "VALID")
        capture["sidecars"] = {
            "capture.json": self._file_integrity_record(capture_path),
            "qc.json": self._file_integrity_record(qc_path),
            COMMIT_RECORD_FILENAME: self._file_integrity_record(commit_path),
        }
        return capture

    def _subject_id_from_attempt_path(self, directory: Path, staging: bool) -> str:
        resolved = directory.resolve()
        for parent in (resolved, *resolved.parents):
            if parent.parent == self.subjects_dir.resolve():
                return parent.name
        raise ProtocolStoreError(
            f"attempt directory is not inside subjects root: {directory}"
        )

    def _promote_durable_attempt(
        self,
        state: MutableMapping[str, Any],
        durable: Mapping[str, Any],
    ) -> None:
        attempt_id = str(durable["attempt_id"])
        condition_id = str(durable["condition_id"])
        if condition_id not in state["conditions"]:
            raise ProtocolStoreError(
                f"durable attempt condition is not in frozen snapshot: {condition_id}"
            )
        existing = state["attempts"].get(attempt_id, {})
        merged = {**existing, **self._copy_json(dict(durable))}
        merged.setdefault("validity", "VALID")
        state["attempts"][attempt_id] = merged
        attempt_ids = state["conditions"][condition_id]["attempt_ids"]
        if attempt_id not in attempt_ids:
            attempt_ids.append(attempt_id)

    def _mark_recovered_incomplete_attempt(
        self,
        state: MutableMapping[str, Any],
        attempt: MutableMapping[str, Any],
        *,
        status: str,
        error: str,
    ) -> None:
        if status not in {"ABORTED", "WRITE_FAILED"}:
            raise ProtocolStoreError(f"invalid recovery failure status: {status}")
        attempt["status"] = status
        attempt["committed_at"] = attempt.get("committed_at") or self._now()
        attempt["quality_status"] = "FAIL"
        attempt["disposition"] = "BAD"
        attempt["review_status"] = "NOT_APPLICABLE"
        attempt["validity"] = "INVALID"
        attempt["error"] = error

    def _rebuild_condition_states(self, state: MutableMapping[str, Any]) -> bool:
        changed = False
        for condition_id, condition_state in state["conditions"].items():
            attempts = [
                state["attempts"][attempt_id]
                for attempt_id in condition_state.get("attempt_ids", [])
                if attempt_id in state["attempts"]
            ]
            acceptable = [
                attempt
                for attempt in attempts
                if attempt.get("status") == "COMMITTED"
                and attempt.get("validity", "VALID") != "INVALIDATED"
                and (
                    attempt.get("quality_status") == "PASS"
                    or (
                        attempt.get("quality_status") == "WARN"
                        and attempt.get("review_status") == "ACCEPTED"
                    )
                )
            ]
            current_accepted = condition_state.get("accepted_attempt_id")
            accepted = next(
                (
                    attempt
                    for attempt in acceptable
                    if attempt.get("attempt_id") == current_accepted
                ),
                acceptable[0] if acceptable else None,
            )
            # Follow the explicit PASS supersession chain.  This is essential
            # when a durable retake is recovered from disk while subject state
            # still points at the previously accepted PASS.
            visited_supersession_ids: set[str] = set()
            while accepted is not None:
                accepted_id = str(accepted.get("attempt_id") or "")
                if not accepted_id or accepted_id in visited_supersession_ids:
                    break
                visited_supersession_ids.add(accepted_id)
                superseding = [
                    attempt
                    for attempt in acceptable
                    if attempt.get("supersedes_attempt_id")
                    == accepted_id
                    and attempt.get("attempt_id") not in visited_supersession_ids
                ]
                if not superseding:
                    break
                accepted = superseding[-1]
            pending_review = [
                attempt
                for attempt in attempts
                if attempt.get("status") == "COMMITTED"
                and attempt.get("quality_status") == "WARN"
                and attempt.get("review_status") in {None, "PENDING"}
            ]
            if pending_review:
                new_status = "REVIEW_REQUIRED"
                new_accepted = (
                    accepted["attempt_id"] if accepted is not None else None
                )
            elif accepted is not None:
                new_status = "CAPTURED"
                new_accepted = accepted["attempt_id"]
            elif any(attempt.get("status") == "PENDING" for attempt in attempts):
                new_status = "IN_PROGRESS"
                new_accepted = None
            elif attempts:
                new_status = "NEEDS_RETAKE"
                new_accepted = None
            else:
                new_status = "PENDING"
                new_accepted = None
            if (
                condition_state.get("status") != new_status
                or condition_state.get("accepted_attempt_id") != new_accepted
            ):
                condition_state["status"] = new_status
                condition_state["accepted_attempt_id"] = new_accepted
                changed = True
        return changed

    def _reconcile_anthropometry(
        self, state: MutableMapping[str, Any]
    ) -> Tuple[bool, List[str]]:
        definitions = self._measurement_definitions_from_state(state)
        required_measurement_ids = tuple(
            measurement_id
            for measurement_id in ALL_MEASUREMENT_IDS
            if definitions[measurement_id]["required"]
        )
        directory = self._subject_dir(state["subject_id"]) / "meta" / "anthropometry"
        if not directory.exists():
            return False, []
        current = state.get("anthropometry", {})
        current_path_text = current.get("latest_path")
        if current.get("status") == "COMPLETE" and current_path_text:
            current_path = self._safe_subject_relative_path(
                state["subject_id"], str(current_path_text)
            )
            if not current_path.is_file():
                return False, [
                    f"committed anthropometry file is missing: {current_path_text}"
                ]
            expected_hash = str(current.get("latest_sha256") or "")
            actual_hash = self._sha256(current_path)
            if not expected_hash or actual_hash != expected_hash:
                # Never re-sign a state-known revision after bit rot/tampering.
                return False, [
                    f"CORRUPTED committed anthropometry sha256 mismatch: {current_path_text}"
                ]

        valid: Dict[int, Tuple[Path, Dict[str, Any]]] = {}
        errors: List[str] = []
        for path in sorted(directory.glob("anthropometry_*.json")):
            try:
                record = self._read_json(path)
                revision = int(record.get("revision"))
                if record.get("subject_id") != state["subject_id"]:
                    raise ProtocolStoreError("anthropometry subject_id mismatch")
                supplied_content_hash = str(record.get("content_sha256") or "")
                unhashed = dict(record)
                unhashed.pop("content_sha256", None)
                if not supplied_content_hash or supplied_content_hash != self._canonical_sha256(
                    unhashed
                ):
                    raise ProtocolStoreError(
                        "orphan anthropometry lacks a valid embedded content hash"
                    )
                metadata = record.get("metadata")
                if not isinstance(metadata, Mapping):
                    raise ProtocolStoreError("anthropometry metadata must be a mapping")
                try:
                    self._validate_anthropometry_metadata(state, metadata)
                    self._normalize_anthropometry(
                        record.get("records", []),
                        definitions=definitions,
                    )
                except ProtocolValidationError as exc:
                    raise ProtocolStoreError(
                        f"anthropometry protocol validation failed: {exc}"
                    ) from exc
                present = {
                    (str(item.get("measurement_id")), str(item.get("field_name")))
                    for item in record.get("records", [])
                    if isinstance(item, Mapping)
                }
                missing = [
                    f"{measurement_id}::{field_name}"
                    for measurement_id in required_measurement_ids
                    for field_name in definitions[measurement_id]["fields"]
                    if (measurement_id, field_name) not in present
                ]
                if missing:
                    raise ProtocolStoreError(
                        "anthropometry core fields missing: " + ", ".join(missing)
                    )
                if revision in valid:
                    raise ProtocolStoreError(f"duplicate anthropometry revision: {revision}")
                valid[revision] = (path, record)
            except Exception as exc:
                errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
        if not valid:
            return False, errors
        revisions = sorted(valid)
        if revisions != list(range(1, revisions[-1] + 1)):
            errors.append("anthropometry revisions are not contiguous")
            return False, errors
        latest_revision = revisions[-1]
        latest_path = valid[latest_revision][0]
        current_revision = int(current.get("latest_revision") or 0)
        if latest_revision <= current_revision:
            return False, errors
        relative = latest_path.relative_to(self._subject_dir(state["subject_id"])).as_posix()
        expected = {
            "status": "COMPLETE",
            "revision_count": latest_revision,
            "latest_revision": latest_revision,
            "latest_path": relative,
            "latest_sha256": self._sha256(latest_path),
            "completed_measurement_ids": list(required_measurement_ids),
        }
        if state.get("anthropometry") == expected:
            return False, errors
        state["anthropometry"] = expected
        return True, errors

    def _repair_completion_report(self, state: Mapping[str, Any]) -> bool:
        path = self._completion_report_path(state["subject_id"])
        if state.get("status") != "COMPLETE":
            return False
        rebuild = True
        if path.exists():
            try:
                current = self._read_json(path)
                fresh = self._build_completion_report(state)
                rebuild = not self._completion_report_is_current(current, fresh)
            except ProtocolStoreError:
                rebuild = True
        if rebuild:
            self._atomic_write_json(path, self._build_completion_report(state))
        return rebuild

    def _completion_report_is_current(
        self,
        existing: Mapping[str, Any],
        fresh: Mapping[str, Any],
    ) -> bool:
        supplied_hash = str(existing.get("content_sha256") or "")
        unhashed = dict(existing)
        unhashed.pop("content_sha256", None)
        if not supplied_hash or supplied_hash != self._canonical_sha256(unhashed):
            return False
        existing_semantics = {
            key: value
            for key, value in existing.items()
            if key not in {"generated_at", "content_sha256"}
        }
        fresh_semantics = {
            key: value
            for key, value in fresh.items()
            if key not in {"generated_at", "content_sha256"}
        }
        return existing_semantics == fresh_semantics

    def _apply_review_to_state(
        self,
        state: MutableMapping[str, Any],
        review: Mapping[str, Any],
    ) -> None:
        attempt_id = str(review.get("attempt_id", ""))
        condition_id = str(review.get("condition_id", ""))
        attempt = state.get("attempts", {}).get(attempt_id)
        if not isinstance(attempt, MutableMapping):
            raise ProtocolStoreError(f"review references unknown attempt: {attempt_id}")
        if attempt.get("condition_id") != condition_id:
            raise ProtocolStoreError("review condition does not match capture attempt")
        if attempt.get("status") != "COMMITTED" or attempt.get("quality_status") != "WARN":
            raise ProtocolStoreError("review target must be a committed WARN attempt")
        supplied_content_hash = review.get("content_sha256")
        if supplied_content_hash:
            unhashed = {
                key: value
                for key, value in review.items()
                if key not in {"content_sha256", "path", "file_sha256"}
            }
            if str(supplied_content_hash) != self._canonical_sha256(unhashed):
                raise ProtocolStoreError("review content hash mismatch")
        decision = str(review.get("decision", "")).upper()
        expected_review_status = "ACCEPTED" if decision == "ACCEPT" else "REJECTED"
        if decision not in {"ACCEPT", "REJECT"}:
            raise ProtocolStoreError("invalid persisted review decision")
        if review.get("review_status") != expected_review_status:
            raise ProtocolStoreError("persisted review status/decision mismatch")

        condition_state = state["conditions"][condition_id]
        accepted_id = condition_state.get("accepted_attempt_id")
        if accepted_id and accepted_id != attempt_id:
            accepted = state["attempts"].get(accepted_id, {})
            retained_prior = (
                accepted_id == attempt.get("prior_accepted_attempt_id")
                and not attempt.get("invalidate_prior")
                and accepted.get("quality_status") == "PASS"
                and accepted.get("validity", "VALID") != "INVALIDATED"
            )
            if not retained_prior and accepted.get("quality_status") == "PASS":
                raise ProtocolStoreError("review cannot replace accepted PASS attempt")
            if not retained_prior:
                raise ProtocolStoreError("review conflicts with another accepted attempt")
        attempt["review_status"] = expected_review_status
        attempt["review"] = self._copy_json(dict(review))
        if decision == "ACCEPT":
            condition_state["status"] = "CAPTURED"
            condition_state["accepted_attempt_id"] = attempt_id
        else:
            prior_id = attempt.get("prior_accepted_attempt_id")
            prior = state["attempts"].get(prior_id, {}) if prior_id else {}
            if (
                prior_id
                and not attempt.get("invalidate_prior")
                and prior.get("quality_status") == "PASS"
                and prior.get("validity", "VALID") != "INVALIDATED"
            ):
                condition_state["status"] = "CAPTURED"
                condition_state["accepted_attempt_id"] = prior_id
            else:
                condition_state["status"] = "NEEDS_RETAKE"
                condition_state["accepted_attempt_id"] = None

    def _state_with_anthropometry(self, state: Mapping[str, Any]) -> Dict[str, Any]:
        enriched = self._copy_json(state)
        definitions = self._measurement_definitions_from_state(enriched)
        required_measurement_ids = [
            measurement_id
            for measurement_id in ALL_MEASUREMENT_IDS
            if definitions[measurement_id]["required"]
        ]
        anthro_state = dict(enriched.get("anthropometry", {}))
        latest_path = anthro_state.get("latest_path")
        latest_record: Optional[Dict[str, Any]] = None
        if latest_path:
            path = self._subject_dir(enriched["subject_id"]) / Path(latest_path)
            if not path.is_file():
                raise ProtocolStoreError(
                    f"latest anthropometry revision is missing: {latest_path}"
                )
            expected_hash = anthro_state.get("latest_sha256")
            if expected_hash and self._sha256(path) != expected_hash:
                raise ProtocolStoreError(
                    f"latest anthropometry revision hash mismatch: {latest_path}"
                )
            latest_record = self._read_json(path)
        anthro_state["complete"] = anthro_state.get("status") == "COMPLETE"
        anthro_state["missing_required"] = (
            [] if anthro_state["complete"] else required_measurement_ids
        )
        anthro_state["records"] = latest_record.get("records", []) if latest_record else []
        anthro_state["measurements"] = (
            latest_record.get("measurements", {}) if latest_record else {}
        )
        anthro_state["metadata"] = (
            latest_record.get("metadata", {}) if latest_record else {}
        )
        anthro_state["warnings"] = (
            latest_record.get("warnings", []) if latest_record else []
        )
        enriched["anthropometry"] = anthro_state
        return enriched

    def _accepted_attempt_integrity_errors(
        self, state: Mapping[str, Any], condition_id: str
    ) -> List[str]:
        errors: List[str] = []
        condition_state = state.get("conditions", {}).get(condition_id, {})
        attempt_id = condition_state.get("accepted_attempt_id")
        if not attempt_id:
            return ["missing accepted_attempt_id"]
        attempt = state.get("attempts", {}).get(attempt_id)
        if not isinstance(attempt, Mapping):
            return [f"accepted attempt not found: {attempt_id}"]
        if attempt.get("status") != "COMMITTED":
            errors.append(f"accepted attempt status is {attempt.get('status')}")
        quality = attempt.get("quality_status")
        if quality == "WARN":
            if attempt.get("review_status") != "ACCEPTED":
                errors.append("accepted WARN attempt lacks ACCEPTED manual review")
            review = attempt.get("review")
            if not isinstance(review, Mapping):
                errors.append("accepted WARN attempt has no review record")
            else:
                review_files = sorted(
                    (
                        self._subject_dir(state["subject_id"])
                        / "meta"
                        / "reviews"
                        / str(attempt_id)
                    ).glob("*.json")
                )
                if len(review_files) != 1:
                    errors.append(
                        f"accepted WARN attempt must have exactly one review sidecar; "
                        f"found {len(review_files)}"
                    )
                review_path = review.get("path")
                if not review_path:
                    errors.append("accepted WARN review path is missing")
                else:
                    try:
                        absolute_review = self._safe_subject_relative_path(
                            state["subject_id"], str(review_path)
                        )
                        if not absolute_review.is_file():
                            errors.append(f"review file is missing: {review_path}")
                        else:
                            actual_file_sha = self._sha256(absolute_review)
                            if not review.get("file_sha256") or actual_file_sha != review.get(
                                "file_sha256"
                            ):
                                errors.append(f"review sha256 mismatch: {review_path}")
                            persisted_review = self._read_json(absolute_review)
                            supplied_content_hash = str(
                                persisted_review.get("content_sha256") or ""
                            )
                            unhashed_review = dict(persisted_review)
                            unhashed_review.pop("content_sha256", None)
                            if not supplied_content_hash or supplied_content_hash != self._canonical_sha256(
                                unhashed_review
                            ):
                                errors.append(
                                    f"review embedded content hash mismatch: {review_path}"
                                )
                            state_review_payload = {
                                key: value
                                for key, value in review.items()
                                if key not in {"path", "file_sha256"}
                            }
                            if state_review_payload != persisted_review:
                                errors.append(
                                    f"state review differs from review sidecar: {review_path}"
                                )
                            if (
                                persisted_review.get("decision") != "ACCEPT"
                                or persisted_review.get("review_status") != "ACCEPTED"
                                or persisted_review.get("review_id") != review.get("review_id")
                                or persisted_review.get("attempt_id") != attempt_id
                                or persisted_review.get("condition_id") != condition_id
                                or persisted_review.get("subject_id") != state.get("subject_id")
                            ):
                                errors.append(
                                    f"review sidecar is not an ACCEPT decision for this attempt: "
                                    f"{review_path}"
                                )
                    except ProtocolStoreError as exc:
                        errors.append(str(exc))
        elif quality != "PASS":
            errors.append(f"accepted attempt QC is {quality}")

        try:
            attempt_condition = attempt.get("condition")
            if not isinstance(attempt_condition, Mapping):
                raise ProtocolStoreError("accepted attempt has no canonical condition")
            attempt_dir = self._attempt_final_dir(
                state["subject_id"],
                attempt_condition,
                str(attempt_id),
            )
            durable = self._verify_attempt_directory(
                attempt_dir,
                expected_attempt_id=str(attempt_id),
                expected_condition_id=condition_id,
                staging=False,
                subject_state=state,
            )
            for immutable_field in (
                "frames",
                "files",
                "qc",
                "camera_metadata",
                "quality_status",
                "condition",
                "sidecars",
            ):
                if durable.get(immutable_field) != attempt.get(immutable_field):
                    errors.append(
                        f"state {immutable_field} differs from durable sidecars"
                    )
        except (ProtocolStoreError, KeyError) as exc:
            errors.append(f"attempt sidecar integrity failed: {exc}")

        frame_records = attempt.get("frames")
        if not isinstance(frame_records, list) or len(frame_records) != FRAME_COUNT:
            errors.append(
                f"expected {FRAME_COUNT} frame records, got "
                f"{len(frame_records) if isinstance(frame_records, list) else 0}"
            )

        required_modalities = {"rgb", "depth_raw", "depth_aligned"}
        camera_metadata = attempt.get("camera_metadata", {})
        if isinstance(camera_metadata, Mapping):
            ir_names = camera_metadata.get("enabled_ir_streams")
            if isinstance(ir_names, Sequence) and not isinstance(ir_names, (str, bytes)):
                for name in ir_names:
                    required_modalities.add(
                        {"left": "ir_left", "right": "ir_right", "single": "ir"}.get(
                            str(name), str(name)
                        )
                    )
            stream_profiles = camera_metadata.get("stream_profiles", {})
            if isinstance(stream_profiles, Mapping):
                if "infrared_left" in stream_profiles:
                    required_modalities.add("ir_left")
                if "infrared_right" in stream_profiles:
                    required_modalities.add("ir_right")

        modalities_by_frame = {index: set() for index in range(1, FRAME_COUNT + 1)}
        attempt_dirs = set()
        files = attempt.get("files")
        if not isinstance(files, list) or not files:
            errors.append("accepted attempt has no file records")
            files = []
        base = self.base_dir.resolve()
        for file_record in files:
            if not isinstance(file_record, Mapping):
                errors.append("malformed file record")
                continue
            try:
                frame_index = int(file_record["frame_index"])
                modality = str(file_record["modality"])
                relative_path = Path(str(file_record["path"]))
                absolute_path = (self.base_dir / relative_path).resolve()
                absolute_path.relative_to(base)
            except Exception as exc:
                errors.append(f"invalid file record: {exc}")
                continue
            if frame_index in modalities_by_frame:
                modalities_by_frame[frame_index].add(modality)
            else:
                errors.append(f"invalid frame_index in file record: {frame_index}")
            attempt_dirs.add(absolute_path.parent.parent)
            if not absolute_path.is_file():
                errors.append(f"missing file: {relative_path.as_posix()}")
                continue
            expected_size = file_record.get("size_bytes")
            if expected_size is not None and absolute_path.stat().st_size != int(expected_size):
                errors.append(f"size mismatch: {relative_path.as_posix()}")
            expected_hash = str(file_record.get("sha256") or "")
            if not expected_hash or self._sha256(absolute_path) != expected_hash:
                errors.append(f"sha256 mismatch: {relative_path.as_posix()}")

        for frame_index, modalities in modalities_by_frame.items():
            missing = sorted(required_modalities - modalities)
            if missing:
                errors.append(
                    f"F{frame_index:02d} missing modalities: {', '.join(missing)}"
                )
        if len(attempt_dirs) != 1:
            errors.append("attempt files do not share one attempt directory")
        else:
            marker = next(iter(attempt_dirs)) / "commit.json"
            if not marker.is_file():
                errors.append("commit.json is missing")
            else:
                try:
                    marker_payload = self._read_json(marker)
                    if marker_payload.get("attempt_id") != attempt_id:
                        errors.append("commit.json attempt_id mismatch")
                    if marker_payload.get("status") != "COMMITTED":
                        errors.append("commit.json is not COMMITTED")
                except ProtocolStoreError as exc:
                    errors.append(str(exc))
        return errors

    def _anthropometry_integrity_errors(self, state: Mapping[str, Any]) -> List[str]:
        try:
            definitions = self._measurement_definitions_from_state(state)
        except ProtocolStoreError as exc:
            return [f"anthropometry frozen-definition validation failed: {exc}"]
        required_measurement_ids = tuple(
            measurement_id
            for measurement_id in ALL_MEASUREMENT_IDS
            if definitions[measurement_id]["required"]
        )
        anthropometry = state.get("anthropometry", {})
        if anthropometry.get("status") != "COMPLETE":
            return []
        latest_path = anthropometry.get("latest_path")
        if not latest_path:
            return ["anthropometry latest_path is missing"]
        path = self._subject_dir(state["subject_id"]) / Path(str(latest_path))
        if not path.is_file():
            return [f"anthropometry file is missing: {latest_path}"]
        expected_hash = str(anthropometry.get("latest_sha256") or "")
        if not expected_hash or self._sha256(path) != expected_hash:
            return [f"anthropometry sha256 mismatch: {latest_path}"]
        try:
            record = self._read_json(path)
        except ProtocolStoreError as exc:
            return [str(exc)]
        supplied_content_hash = str(record.get("content_sha256") or "")
        unhashed = dict(record)
        unhashed.pop("content_sha256", None)
        if not supplied_content_hash or supplied_content_hash != self._canonical_sha256(
            unhashed
        ):
            return [f"anthropometry embedded content hash mismatch: {latest_path}"]
        metadata = record.get("metadata")
        if not isinstance(metadata, Mapping):
            return ["anthropometry metadata must be a mapping"]
        try:
            operator_id = self._validate_anthropometry_metadata(state, metadata)
            self._normalize_anthropometry(
                record.get("records", []),
                definitions=definitions,
            )
        except ProtocolValidationError as exc:
            return [f"anthropometry validation failed: {exc}"]
        present = {
            (str(item.get("measurement_id")), str(item.get("field_name")))
            for item in record.get("records", [])
            if isinstance(item, Mapping)
        }
        missing = []
        for measurement_id in required_measurement_ids:
            for field_name in definitions[measurement_id]["fields"]:
                if (measurement_id, field_name) not in present:
                    missing.append(f"{measurement_id}::{field_name}")
        if bool(
            self._capture_policy_from_state(state).get("strict_qc_contract", False)
            or self._capture_policy_from_state(state).get(
                "require_anthropometry_equipment", False
            )
        ):
            for item in record.get("records", []):
                if not isinstance(item, Mapping):
                    continue
                if item.get("operator_id") != operator_id or not item.get("recorded_at"):
                    missing.append(
                        f"{item.get('measurement_id')}::{item.get('field_name')} audit fields"
                    )
        return ["anthropometry required records missing: " + ", ".join(missing)] if missing else []

    def _protocol_snapshot_integrity_errors(
        self, state: Mapping[str, Any]
    ) -> List[str]:
        try:
            self._read_protocol_snapshot_file(state)
            return []
        except ProtocolStoreError as exc:
            return [f"protocol snapshot integrity failed: {exc}"]

    def _manifest_integrity_errors(
        self, state: Mapping[str, Any]
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        path = self._manifest_path(state["subject_id"])
        if not path.is_file():
            return ["subject manifest is missing"], []
        try:
            events = self._read_manifest_events(path)
        except ProtocolStoreError as exc:
            return [str(exc)], []
        errors: List[str] = []
        for event in events:
            event_subject = event.get("subject_id")
            if event_subject is not None and event_subject != state["subject_id"]:
                errors.append(
                    f"manifest event {event.get('event_index')} subject_id mismatch"
                )

        created_events = [item for item in events if item.get("event") == "SUBJECT_CREATED"]
        if len(created_events) != 1:
            errors.append("manifest must contain exactly one SUBJECT_CREATED event")
        else:
            created = created_events[0]
            snapshot_file = state.get("protocol_snapshot_file", {})
            snapshot = state.get("protocol_snapshot", {})
            if created.get("protocol_snapshot_sha256") != snapshot_file.get("sha256"):
                errors.append("SUBJECT_CREATED protocol snapshot file hash mismatch")
            if created.get("protocol_snapshot_content_sha256") != snapshot.get("sha256"):
                errors.append("SUBJECT_CREATED protocol snapshot content hash mismatch")
            if created.get("expected_condition_ids") != state.get(
                "expected_condition_ids"
            ):
                errors.append("SUBJECT_CREATED expected conditions mismatch")

        reconciled_actions = [
            action
            for event in events
            if event.get("event") == "SUBJECT_RECONCILED"
            for action in event.get("actions", [])
            if isinstance(action, Mapping)
        ]
        for condition_id in state.get("expected_condition_ids", []):
            condition_state = state.get("conditions", {}).get(condition_id, {})
            attempt_id = condition_state.get("accepted_attempt_id")
            if not attempt_id:
                continue
            attempt = state.get("attempts", {}).get(attempt_id, {})
            commit_events = [
                item
                for item in events
                if item.get("event") == "CAPTURE_ATTEMPT_COMMITTED"
                and item.get("attempt_id") == attempt_id
            ]
            matching_commit = any(
                item.get("condition_id") == condition_id
                and item.get("quality_status") == attempt.get("quality_status")
                and item.get("sidecars") == attempt.get("sidecars")
                for item in commit_events
            )
            matching_recovery = any(
                action.get("attempt_id") == attempt_id
                and action.get("condition_id") == condition_id
                and action.get("quality_status") == attempt.get("quality_status")
                and action.get("sidecars") == attempt.get("sidecars")
                and action.get("action") in {"COMMITTED", "IMPORTED_COMMIT"}
                for action in reconciled_actions
            )
            if not matching_commit and not matching_recovery:
                errors.append(
                    f"manifest lacks matching durable commit evidence: {attempt_id}"
                )
            if attempt.get("quality_status") == "WARN":
                review = attempt.get("review", {})
                review_id = review.get("review_id") if isinstance(review, Mapping) else None
                matching_review = any(
                    item.get("event") == "CAPTURE_ATTEMPT_REVIEWED"
                    and item.get("attempt_id") == attempt_id
                    and item.get("review_id") == review_id
                    and item.get("file_sha256") == review.get("file_sha256")
                    and item.get("decision") == "ACCEPT"
                    and item.get("review_status") == "ACCEPTED"
                    and all(item.get(key) == value for key, value in review.items())
                    for item in events
                ) or any(
                    action.get("action") == "REVIEW_REPLAYED"
                    and action.get("attempt_id") == attempt_id
                    and action.get("review_id") == review_id
                    and action.get("review_file_sha256") == review.get("file_sha256")
                    and action.get("decision") == "ACCEPT"
                    and action.get("review_status") == "ACCEPTED"
                    and action.get("content_sha256") == review.get("content_sha256")
                    for action in reconciled_actions
                )
                if not matching_review:
                    errors.append(
                        f"manifest lacks matching manual review evidence: {attempt_id}"
                    )

        anthropometry = state.get("anthropometry", {})
        if anthropometry.get("status") == "COMPLETE":
            matching_anthro = any(
                item.get("event") == "ANTHROPOMETRY_SAVED"
                and item.get("revision") == anthropometry.get("latest_revision")
                and item.get("path") == anthropometry.get("latest_path")
                and item.get("sha256") == anthropometry.get("latest_sha256")
                for item in events
            ) or any(
                action.get("action") == "ANTHROPOMETRY_RECOVERED"
                and action.get("revision") == anthropometry.get("latest_revision")
                and action.get("path") == anthropometry.get("latest_path")
                and action.get("sha256") == anthropometry.get("latest_sha256")
                for action in reconciled_actions
            )
            if not matching_anthro:
                errors.append("manifest lacks matching anthropometry evidence")

        if state.get("status") == "COMPLETE":
            matching_completion = any(
                item.get("event") == "SUBJECT_COMPLETED"
                and item.get("state_revision") == state.get("revision")
                and item.get("timestamp") == state.get("completed_at")
                for item in events
            )
            if not matching_completion:
                errors.append("manifest lacks matching SUBJECT_COMPLETED evidence")
        return errors, events

    def _build_completion_report(self, state: Mapping[str, Any]) -> Dict[str, Any]:
        expected = list(state["expected_condition_ids"])
        try:
            definitions = self._measurement_definitions_from_state(state)
            required_measurement_ids = tuple(
                measurement_id
                for measurement_id in ALL_MEASUREMENT_IDS
                if definitions[measurement_id]["required"]
            )
        except ProtocolStoreError:
            # The integrity report below will retain the frozen-definition error.
            required_measurement_ids = CORE_MEASUREMENT_IDS
        integrity_by_condition: Dict[str, List[str]] = {}
        captured = []
        for condition_id in expected:
            if state["conditions"][condition_id]["status"] != "CAPTURED":
                continue
            condition_errors = self._accepted_attempt_integrity_errors(state, condition_id)
            if condition_errors:
                integrity_by_condition[condition_id] = condition_errors
            else:
                captured.append(condition_id)
        missing = [condition_id for condition_id in expected if condition_id not in captured]
        failed_conditions = [
            condition_id
            for condition_id in expected
            if state["conditions"][condition_id]["status"] == "NEEDS_RETAKE"
        ]
        failed_attempts = [
            attempt["attempt_id"]
            for attempt in state["attempts"].values()
            if attempt.get("quality_status") == "FAIL" or attempt["status"] == "WRITE_FAILED"
        ]
        warning_attempts = [
            attempt["attempt_id"]
            for attempt in state["attempts"].values()
            if attempt.get("quality_status") == "WARN"
        ]
        anthro = state.get("anthropometry", {})
        anthro_complete = anthro.get("status") == "COMPLETE"
        anthropometry_integrity_errors = self._anthropometry_integrity_errors(state)
        protocol_snapshot_integrity_errors = self._protocol_snapshot_integrity_errors(
            state
        )
        manifest_integrity_errors, manifest_events = self._manifest_integrity_errors(
            state
        )
        integrity_errors = [
            f"{condition_id}: {error}"
            for condition_id, errors in integrity_by_condition.items()
            for error in errors
        ] + anthropometry_integrity_errors + protocol_snapshot_integrity_errors + manifest_integrity_errors
        ready = not missing and anthro_complete and not integrity_errors
        if state["status"] == "COMPLETE" and ready:
            status = "COMPLETE"
        elif state["status"] == "COMPLETE":
            status = "CORRUPTED"
        elif ready:
            status = "READY"
        else:
            status = "INCOMPLETE"
        report = {
            "schema_version": "1.0",
            "generated_at": self._now(),
            "subject_id": state["subject_id"],
            "dataset_phase": state["dataset_phase"],
            "protocol_version": state["protocol_version"],
            "profile_id": state["profile_id"],
            "status": status,
            "ready_to_complete": ready,
            "expected_conditions": len(expected),
            "captured_conditions": len(captured),
            "expected_condition_ids": expected,
            "captured_condition_ids": captured,
            "missing": len(missing),
            "missing_condition_ids": missing,
            "invalid_condition_ids": list(integrity_by_condition),
            "integrity_errors": integrity_errors,
            "failed": len(failed_conditions),
            "failed_condition_ids": failed_conditions,
            "failed_attempts": failed_attempts,
            "warnings": len(warning_attempts),
            "warning_attempts": warning_attempts,
            "anthro_required": len(required_measurement_ids),
            "anthro_completed": len(required_measurement_ids) if anthro_complete else 0,
            "anthropometry_complete": anthro_complete,
            "anthropometry_revision": anthro.get("latest_revision"),
            "subject_state_revision": state["revision"],
            "completed_at": state.get("completed_at"),
            "protocol_snapshot_sha256": state.get("protocol_snapshot", {}).get(
                "sha256"
            ),
            "manifest_event_count": len(manifest_events),
            "manifest_head_sha256": (
                manifest_events[-1].get("event_sha256") if manifest_events else None
            ),
            "manifest_file_sha256": (
                self._sha256(self._manifest_path(state["subject_id"]))
                if manifest_events
                else None
            ),
        }
        report["content_sha256"] = self._canonical_sha256(report)
        return report

    def _record_write_failure(
        self, state: MutableMapping[str, Any], attempt: MutableMapping[str, Any], exc: Exception
    ) -> None:
        attempt["status"] = "WRITE_FAILED"
        attempt["committed_at"] = self._now()
        attempt["quality_status"] = "FAIL"
        attempt["disposition"] = "BAD"
        attempt["review_status"] = "NOT_APPLICABLE"
        attempt["error"] = f"{type(exc).__name__}: {exc}"
        condition_state = state["conditions"][attempt["condition_id"]]
        condition_state["status"] = (
            "CAPTURED"
            if condition_state.get("accepted_attempt_id")
            else "NEEDS_RETAKE"
        )
        self._advance_state(state)
        try:
            self._atomic_append_jsonl(
                self._manifest_path(state["subject_id"]),
                {
                    "event": "CAPTURE_ATTEMPT_WRITE_FAILED",
                    "timestamp": attempt["committed_at"],
                    "subject_id": state["subject_id"],
                    "attempt_id": attempt["attempt_id"],
                    "condition_id": attempt["condition_id"],
                    "error": attempt["error"],
                },
            )
            self._atomic_write_json(self._state_path(state["subject_id"]), state)
        except Exception:
            # Preserve the original image-write exception for the caller.
            pass

    @staticmethod
    def _quality_status(qc: Mapping[str, Any]) -> str:
        explicit = qc.get("status", qc.get("overall_status", qc.get("result")))
        if explicit is not None:
            value = str(ProtocolStore._enum_value(explicit)).strip().upper()
            aliases = {
                "OK": "PASS",
                "PASSED": "PASS",
                "WARNING": "WARN",
                "REVIEW_REQUIRED": "WARN",
                "FAILED": "FAIL",
                "BAD": "FAIL",
            }
            value = aliases.get(value, value)
            if value not in {"PASS", "WARN", "FAIL"}:
                raise ProtocolValidationError("qc status must be PASS, WARN, or FAIL")
            return value
        seen_warn = False
        for value in qc.values():
            if isinstance(value, str):
                normalized = value.strip().upper()
                if normalized in {"FAIL", "FAILED", "BAD"}:
                    return "FAIL"
                if normalized in {"WARN", "WARNING"}:
                    seen_warn = True
        return "WARN" if seen_warn else "PASS"

    def _read_state(self, subject_id: str) -> Dict[str, Any]:
        path = self._state_path(subject_id)
        if not path.exists():
            raise SubjectNotFoundError(f"subject not found: {subject_id}")
        state = self._read_json(path)
        if state.get("subject_id") != subject_id:
            raise ProtocolStoreError(f"subject state identity mismatch: {subject_id}")
        return state

    def _replayable_completion_event(
        self, state: Mapping[str, Any]
    ) -> Optional[Dict[str, Any]]:
        if state.get("status") != "ACTIVE":
            return None
        path = self._manifest_path(str(state.get("subject_id") or ""))
        if not path.is_file():
            return None
        events = self._read_manifest_events(path)
        expected_revision = int(state.get("revision", 0)) + 1
        matches = [
            event
            for event in events
            if event.get("event") == "SUBJECT_COMPLETED"
            and event.get("subject_id") == state.get("subject_id")
            and event.get("state_revision") == expected_revision
            and isinstance(event.get("timestamp"), str)
            and bool(event.get("timestamp"))
        ]
        return matches[-1] if matches else None

    def _ensure_active(self, state: Mapping[str, Any]) -> None:
        if state.get("status") == "COMPLETE":
            raise SubjectCompletedError(f"subject is complete: {state.get('subject_id')}")
        if state.get("status") != "ACTIVE":
            raise ProtocolStoreError(f"invalid subject status: {state.get('status')}")
        completion_event = self._replayable_completion_event(state)
        if completion_event is not None:
            raise SubjectCompletedError(
                "subject has a durable completion event pending reconciliation: "
                f"{state.get('subject_id')}"
            )

    def _advance_state(self, state: MutableMapping[str, Any]) -> None:
        state["revision"] = int(state.get("revision", 0)) + 1
        state["updated_at"] = self._now()

    # ------------------------------------------------------------------
    # Atomic filesystem helpers
    # ------------------------------------------------------------------
    def _atomic_write_json(self, path: Path, value: Any) -> None:
        payload = json.dumps(
            self._json_ready(value, str(path)),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        self._atomic_write_bytes(path, payload)

    def _atomic_append_jsonl(self, path: Path, value: Any) -> None:
        current = b""
        existing_events: List[Dict[str, Any]] = []
        if path.exists():
            current = path.read_bytes()
            if current and not current.endswith(b"\n"):
                raise ProtocolStoreError(f"manifest is truncated: {path}")
            existing_events = self._read_manifest_events(path)
        event = self._json_ready(value, str(path))
        if not isinstance(event, dict):
            raise ProtocolStoreError("manifest event must be a JSON object")
        event.pop("event_sha256", None)
        event["event_index"] = len(existing_events) + 1
        event["previous_event_sha256"] = (
            existing_events[-1]["event_sha256"] if existing_events else None
        )
        event["event_sha256"] = self._canonical_sha256(event)
        line = json.dumps(
            event,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        # Logical append plus atomic replacement prevents a partial JSONL line
        # after process interruption.
        self._atomic_write_bytes(path, current + line)

    def _read_manifest_events(self, path: Path) -> List[Dict[str, Any]]:
        try:
            raw_lines = path.read_text("utf-8").splitlines()
        except OSError as exc:
            raise ProtocolStoreError(f"cannot read manifest {path}: {exc}") from exc
        if not raw_lines:
            raise ProtocolStoreError(f"manifest is empty: {path}")
        events: List[Dict[str, Any]] = []
        previous_hash: Optional[str] = None
        for line_number, line in enumerate(raw_lines, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProtocolStoreError(
                    f"manifest JSONL line {line_number} is invalid: {exc}"
                ) from exc
            if not isinstance(event, dict):
                raise ProtocolStoreError(
                    f"manifest line {line_number} must be a JSON object"
                )
            if event.get("event_index") != line_number:
                raise ProtocolStoreError(
                    f"manifest event_index mismatch at line {line_number}"
                )
            if event.get("previous_event_sha256") != previous_hash:
                raise ProtocolStoreError(
                    f"manifest hash chain is broken at line {line_number}"
                )
            supplied_hash = str(event.get("event_sha256") or "")
            unhashed = dict(event)
            unhashed.pop("event_sha256", None)
            if not supplied_hash or supplied_hash != self._canonical_sha256(unhashed):
                raise ProtocolStoreError(
                    f"manifest event hash mismatch at line {line_number}"
                )
            events.append(event)
            previous_hash = supplied_hash
        return events

    @staticmethod
    def _atomic_write_bytes(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temporary, "xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            ProtocolStore._replace_path_with_windows_retry(
                temporary,
                path,
                allow_existing_destination=True,
            )
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ProtocolStoreError(f"cannot read JSON file {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise ProtocolStoreError(f"JSON object expected in {path}")
        return value

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _file_integrity_record(self, path: Path) -> Dict[str, Any]:
        return {
            "size_bytes": path.stat().st_size,
            "sha256": self._sha256(path),
        }

    def _verify_file_integrity_record(
        self,
        path: Path,
        record: Mapping[str, Any],
        *,
        label: str,
    ) -> None:
        if not path.is_file():
            raise ProtocolStoreError(f"{label} is missing")
        try:
            expected_size = int(record.get("size_bytes", -1))
        except (TypeError, ValueError) as exc:
            raise ProtocolStoreError(f"{label} has invalid size inventory") from exc
        if path.stat().st_size != expected_size:
            raise ProtocolStoreError(f"{label} size mismatch")
        expected_hash = str(record.get("sha256") or "")
        if not expected_hash or self._sha256(path) != expected_hash:
            raise ProtocolStoreError(f"{label} sha256 mismatch")

    @staticmethod
    def _canonical_sha256(value: Any) -> str:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    # ------------------------------------------------------------------
    # Generic conversion and validation helpers
    # ------------------------------------------------------------------
    def _subject_dir(self, subject_id: str) -> Path:
        return self.phase_dir / subject_id

    def _state_path(self, subject_id: str) -> Path:
        return self._subject_dir(subject_id) / "meta" / "subject_state.json"

    def _completion_report_path(self, subject_id: str) -> Path:
        return self._subject_dir(subject_id) / "meta" / "subject_completion_report.json"

    def _manifest_path(self, subject_id: str) -> Path:
        return self.manifests_dir / f"{subject_id}.jsonl"

    def _safe_subject_relative_path(self, subject_id: str, relative_path: str) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ProtocolStoreError(f"unsafe subject-relative path: {relative_path}")
        subject_dir = self._subject_dir(subject_id).resolve()
        candidate = (subject_dir / relative).resolve()
        try:
            candidate.relative_to(subject_dir)
        except ValueError as exc:
            raise ProtocolStoreError(
                f"path escapes subject directory: {relative_path}"
            ) from exc
        return candidate

    def _lock_for(self, subject_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._subject_locks.setdefault(subject_id, threading.RLock())

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @staticmethod
    def _new_attempt_id() -> str:
        stamp = datetime.now(timezone.utc).strftime("A%Y%m%dT%H%M%S%fZ")
        return f"{stamp}_{uuid.uuid4().hex[:10]}"

    @staticmethod
    def _validate_safe_id(value: Any, label: str) -> str:
        if not isinstance(value, str):
            raise ProtocolValidationError(f"{label} must be a string")
        value = value.strip()
        if not _SAFE_ID_RE.fullmatch(value) or value in {".", ".."}:
            raise ProtocolValidationError(
                f"{label} must contain only letters, digits, underscore, or hyphen"
            )
        return value

    @staticmethod
    def _validate_nonempty_text(value: Any, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ProtocolValidationError(f"{label} must be a non-empty string")
        value = value.strip()
        if len(value) > 128 or any(ord(char) < 32 for char in value):
            raise ProtocolValidationError(f"invalid {label}")
        return value

    @staticmethod
    def _validate_condition_id(value: str) -> str:
        value = value.strip().upper()
        if not _CONDITION_ID_RE.fullmatch(value):
            raise ProtocolValidationError(
                "condition_id must look like C336L_D2500_V000_LSTD_P1_CF_R01"
            )
        return value

    @staticmethod
    def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
        for key in keys:
            if key not in mapping:
                continue
            value = mapping[key]
            if value is None or (isinstance(value, str) and value == ""):
                continue
            return value
        return None

    @staticmethod
    def _enum_value(value: Any) -> Any:
        return value.value if isinstance(value, Enum) else value

    @classmethod
    def _camera_code(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        value = str(cls._enum_value(value)).strip().upper().replace(" ", "").replace("-", "")
        aliases = {
            "GEMINI336L": "C336L",
            "336L": "C336L",
            "C336L": "C336L",
            "D435I": "CD435I",
            "INTELREALSENSED435I": "CD435I",
            "CD435I": "CD435I",
        }
        value = aliases.get(value, value)
        if not value.startswith("C"):
            value = "C" + value
        if not re.fullmatch(r"C[A-Z0-9]+", value):
            raise ProtocolValidationError(f"invalid camera code: {value}")
        return value

    @classmethod
    def _distance(cls, value: Any) -> Optional[int]:
        if value is None:
            return None
        value = cls._enum_value(value)
        if isinstance(value, str):
            value = value.strip().upper()
            if value.startswith("D"):
                value = value[1:]
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ProtocolValidationError(f"invalid nominal distance: {value}") from exc
        if result < 1 or result > 99999:
            raise ProtocolValidationError("nominal distance must be between 1 and 99999 mm")
        return result

    @classmethod
    def _view(cls, value: Any) -> Optional[int]:
        if value is None:
            return None
        value = cls._enum_value(value)
        if isinstance(value, str):
            value = value.strip().upper()
            if value.startswith("V"):
                value = value[1:]
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ProtocolValidationError(f"invalid view yaw: {value}") from exc
        if result < 0 or result > 359:
            raise ProtocolValidationError("view yaw must be between 0 and 359 degrees")
        return result

    @classmethod
    def _repeat(cls, value: Any) -> Optional[int]:
        if value is None:
            return None
        value = cls._enum_value(value)
        if isinstance(value, str):
            value = value.strip().upper()
            if value.startswith("R"):
                value = value[1:]
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ProtocolValidationError(f"invalid repeat ID: {value}") from exc
        if result < 1 or result > 999:
            raise ProtocolValidationError("repeat ID must be between 1 and 999")
        return result

    @classmethod
    def _prefixed_code(cls, value: Any, prefix: str) -> Optional[str]:
        if value is None:
            return None
        value = str(cls._enum_value(value)).strip().upper().replace(" ", "").replace("-", "")
        if not value.startswith(prefix):
            value = prefix + value
        if not re.fullmatch(re.escape(prefix) + r"[A-Z0-9]+", value):
            raise ProtocolValidationError(f"invalid {prefix} code: {value}")
        return value

    @staticmethod
    def _camera_slug(camera_code: str) -> str:
        return camera_code

    @classmethod
    def _as_mapping(cls, value: Any, label: str) -> Dict[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        if is_dataclass(value) and not isinstance(value, type):
            return asdict(value)
        if hasattr(value, "model_dump") and callable(value.model_dump):
            result = value.model_dump()
            if isinstance(result, Mapping):
                return dict(result)
        if hasattr(value, "to_dict") and callable(value.to_dict):
            result = value.to_dict()
            if isinstance(result, Mapping):
                return dict(result)
        known_attributes = (
            "condition_id",
            "camera_code",
            "camera_id",
            "camera_model",
            "distance_mm",
            "distance_nominal_mm",
            "view_yaw_deg",
            "light_id",
            "pose_id",
            "clothing_id",
            "repeat_id",
            "suite",
            "rgb",
            "color",
            "depth_raw",
            "raw_depth",
            "depth_aligned",
            "aligned_depth",
            "depth",
            "ir",
            "ir_left",
            "ir_right",
            "timestamp",
            "frame_number",
            "depth_scale",
        )
        attributes = {
            attr: getattr(value, attr)
            for attr in known_attributes
            if hasattr(value, attr) and getattr(value, attr) is not None
        }
        if attributes:
            if hasattr(value, "__dict__"):
                attributes.update(
                    {
                        key: item
                        for key, item in vars(value).items()
                        if not key.startswith("_")
                    }
                )
            return attributes
        if hasattr(value, "__dict__"):
            return {
                key: item
                for key, item in vars(value).items()
                if not key.startswith("_")
            }
        raise ProtocolValidationError(f"{label} must be mapping-like")

    @classmethod
    def _json_ready(cls, value: Any, label: str) -> Any:
        def convert(item: Any) -> Any:
            if item is None or isinstance(item, (str, bool, int)):
                return item
            if isinstance(item, float):
                if not math.isfinite(item):
                    raise ProtocolValidationError(f"{label} contains a non-finite number")
                return item
            if isinstance(item, np.generic):
                return convert(item.item())
            if isinstance(item, np.ndarray):
                return convert(item.tolist())
            if isinstance(item, Enum):
                return convert(item.value)
            if isinstance(item, (datetime, date)):
                return item.isoformat()
            if isinstance(item, Path):
                return str(item)
            if is_dataclass(item) and not isinstance(item, type):
                return convert(asdict(item))
            if isinstance(item, Mapping):
                return {str(key): convert(entry) for key, entry in item.items()}
            if isinstance(item, (list, tuple)):
                return [convert(entry) for entry in item]
            if hasattr(item, "model_dump") and callable(item.model_dump):
                return convert(item.model_dump())
            raise ProtocolValidationError(
                f"{label} contains a non-JSON value of type {type(item).__name__}"
            )

        converted = convert(value)
        try:
            json.dumps(converted, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ProtocolValidationError(f"{label} is not JSON serializable") from exc
        return converted

    @staticmethod
    def _copy_json(value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))

    @staticmethod
    def _positive_finite_number(value: Any, label: str) -> float:
        if isinstance(value, bool):
            raise ProtocolValidationError(f"{label} measurements must be numeric")
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ProtocolValidationError(f"{label} measurements must be numeric") from exc
        if not math.isfinite(result) or result <= 0:
            raise ProtocolValidationError(f"{label} measurements must be positive and finite")
        return result

    @staticmethod
    def _is_blank(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return value == ""
        if isinstance(value, (list, tuple, dict)):
            return len(value) == 0
        return False


__all__ = [
    "FRAME_COUNT",
    "CORE_MEASUREMENT_IDS",
    "OPTIONAL_MEASUREMENT_IDS",
    "ProtocolStore",
    "ProtocolStoreError",
    "ProtocolValidationError",
    "SubjectExistsError",
    "SubjectNotFoundError",
    "SubjectCompletedError",
    "IncompleteSubjectError",
]
