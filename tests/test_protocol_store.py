import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np
import backend.core.protocol_store as protocol_store_module

from backend.core.protocol_store import (
    CORE_MEASUREMENT_IDS,
    IncompleteSubjectError,
    ProtocolStore,
    ProtocolStoreError,
    ProtocolValidationError,
    STRICT_QC_REQUIRED_CHECK_COUNTS,
    SubjectCompletedError,
    SubjectExistsError,
)
from backend.protocol.measurements import MEASUREMENTS_BY_ID
from backend.protocol.naming import parse_capture_stem


CONDITION_ID = "C336L_D2500_V000_LSTD_P1_CF_R01"
SECOND_CONDITION_ID = "CD435I_D2500_V000_LSTD_P1_CF_R01"
SECOND_GEMINI_CONDITION_ID = "C336L_D2500_V000_LSTD_P1_CF_R02"


def condition(condition_id=CONDITION_ID, **extra):
    return {"condition_id": condition_id, **extra}


def burst(include_ir=True, stereo_ir=False):
    result = []
    for index in range(5):
        item = {
            "rgb": np.full((8, 10, 3), (index + 1) * 10, dtype=np.uint8),
            "depth_raw": np.full((6, 7), 1000 + index, dtype=np.uint16),
            "depth_aligned": np.full((8, 10), 1100 + index, dtype=np.uint16),
            "timestamp_ns": 1_000_000_000 + index * 100_000_000,
            "frame_number": 100 + index,
            "depth_scale": np.float32(1.0),
        }
        if include_ir and stereo_ir:
            item["ir_left"] = np.full((6, 7), 20 + index, dtype=np.uint8)
            item["ir_right"] = np.full((6, 7), 30 + index, dtype=np.uint8)
        elif include_ir:
            item["ir"] = np.full((6, 7), 20 + index, dtype=np.uint8)
        result.append(item)
    return result


def core_anthropometry():
    values = {}
    for measurement_id in CORE_MEASUREMENT_IDS:
        if measurement_id == "M01":
            values[measurement_id] = [170.0, 170.2]
        elif measurement_id == "M03":
            values[measurement_id] = [40.0, 40.4]
        else:
            values[measurement_id] = {
                "measurement_1": 80.0,
                "measurement_2": 80.8,
            }
    # Optional values are still accepted and retain their repeat semantics.
    values["M02"] = [60.0, 75.0]
    values["M14"] = None
    values["M15"] = ""
    return values


def strict_qc_payload(condition_id=CONDITION_ID):
    policy = {
        "schema_version": "1.0",
        "policy_version": "strict-qc-v1",
        "required_frame_count": 5,
        "anchor_frame": "F03",
        "required_modalities": [
            "rgb",
            "depth_raw",
            "depth_aligned",
            "ir_left",
            "ir_right",
        ],
    }
    encoded = json.dumps(
        policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    per_frame_codes = [
        code
        for code, count in STRICT_QC_REQUIRED_CHECK_COUNTS.items()
        if count == 5
    ]
    checks = [
        {"code": code, "status": "PASS", "frame": f"F{frame_index:02d}"}
        for code in per_frame_codes
        for frame_index in range(1, 6)
    ]
    checks.extend(
        {"code": code, "status": "PASS"}
        for code, count in STRICT_QC_REQUIRED_CHECK_COUNTS.items()
        if count == 1 and code != "HUMAN_CONTENT_MANUAL_REVIEW"
    )
    checks.append(
        {
            "code": "HUMAN_CONTENT_MANUAL_REVIEW",
            "status": "WARN",
            "message": "人工复核 F03",
        }
    )
    return {
        "schema_version": "1.0",
        "status": "WARN",
        "policy_version": "strict-qc-v1",
        "policy_sha256": hashlib.sha256(encoded).hexdigest(),
        "policy_snapshot": policy,
        "condition_id": condition_id,
        "checks": checks,
        "manual_review_required": True,
    }


def strict_camera_metadata(qc):
    return {
        "camera_serial": "TEST123",
        "rgb_color_order": "RGB",
        "qc_policy_version": qc["policy_version"],
        "qc_policy_sha256": qc["policy_sha256"],
        "observed_streams": {
            "color": {"shape": [8, 10, 3], "dtype": "uint8"},
            "depth_raw": {"shape": [6, 7], "dtype": "uint16"},
            "depth_aligned": {"shape": [8, 10], "dtype": "uint16"},
            "ir_left": {"shape": [6, 7], "dtype": "uint8"},
            "ir_right": {"shape": [6, 7], "dtype": "uint8"},
        },
    }


def anthropometry_equipment():
    return {
        "operator_id": "OP01",
        "equipment": {
            "stadiometer_id": "HEIGHT01",
            "scale_id": "SCALE01",
            "tape_id": "TAPE01",
            "anthropometer_id": "ANTHRO01",
            "equipment_check_confirmed": True,
        },
    }


class ProtocolStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.store = ProtocolStore(self.base, dataset_phase="pilot")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def reopen_store(self, **kwargs):
        self.store.close()
        self.store = ProtocolStore(self.base, dataset_phase="pilot", **kwargs)
        return self.store

    def raw_state(self):
        path = self.base / "subjects" / "S0001" / "meta" / "subject_state.json"
        return json.loads(path.read_text("utf-8"))

    def write_raw_state(self, state):
        path = self.base / "subjects" / "S0001" / "meta" / "subject_state.json"
        self.store._atomic_write_json(path, state)

    def create_subject(self, expected=None):
        return self.store.create_subject(
            "S0001",
            protocol_version="1.0",
            profile_id="test_profile",
            subject_metadata={"age_band": "25-34", "operator_id": "OP01"},
            expected_conditions=expected or [CONDITION_ID],
        )

    def create_strict_subject(self):
        qc = strict_qc_payload()
        return self.store.create_subject(
            "S0001",
            protocol_version="1.0",
            profile_id="strict_profile",
            subject_metadata={"operator_id": "OP01"},
            expected_conditions=[CONDITION_ID],
            capture_policy={
                "required_modalities": [
                    "rgb",
                    "depth_raw",
                    "depth_aligned",
                    "ir_left",
                    "ir_right",
                ],
                "optional_modalities": [],
                "qc_policy_version": "strict-qc-v1",
                "warn_requires_manual_review": True,
                "strict_qc_contract": True,
                "require_anthropometry_equipment": True,
                "qc_policy_sha256_by_condition": {
                    CONDITION_ID: qc["policy_sha256"]
                },
                "qc_policy_by_condition": {
                    CONDITION_ID: qc["policy_snapshot"]
                },
                "required_qc_check_counts": STRICT_QC_REQUIRED_CHECK_COUNTS,
            },
        )

    def commit_pass(self, include_ir=True):
        self.store.begin_capture_attempt("S0001", condition())
        return self.store.commit_capture_attempt(
            "S0001",
            condition(),
            burst(include_ir=include_ir),
            qc={"status": "PASS", "body_full_visible": "PASS"},
            camera_metadata={
                "camera_serial": "TEST123",
                "rgb_color_order": "RGB",
                "intrinsics": np.eye(3, dtype=np.float32),
            },
        )

    def test_create_subject_is_append_only_and_validates_identifiers(self):
        state = self.create_subject()
        self.assertEqual(state["status"], "ACTIVE")
        self.assertEqual(state["expected_condition_ids"], [CONDITION_ID])
        self.assertEqual(state["conditions"][CONDITION_ID]["status"], "PENDING")
        with self.assertRaises(SubjectExistsError):
            self.create_subject()
        with self.assertRaises(ProtocolValidationError):
            self.store.create_subject(
                "../S0002", "1.0", "p", {}, expected_conditions=[CONDITION_ID]
            )
        with self.assertRaises(ProtocolValidationError):
            self.store.create_subject("S0002", "1.0", "p", {}, expected_conditions=[])

    def test_list_subjects_returns_sorted_summaries(self):
        self.store.create_subject(
            "S0002", "1.0", "p", {}, expected_conditions=[CONDITION_ID]
        )
        self.create_subject()
        summaries = self.store.list_subjects()
        self.assertEqual({item["subject_id"] for item in summaries}, {"S0001", "S0002"})
        self.assertEqual(
            summaries,
            sorted(summaries, key=lambda item: (item["created_at"], item["subject_id"])),
        )
        self.assertTrue(all(item["expected_conditions"] == 1 for item in summaries))
        self.assertTrue(all(item["dataset_phase"] == "pilot" for item in summaries))

    def test_protocol_provider_can_supply_condition_objects(self):
        class ConditionObject:
            condition_id = SECOND_CONDITION_ID

        class Profile:
            conditions = [ConditionObject()]

        class Registry:
            @staticmethod
            def get_profile(profile_id):
                self.assertEqual(profile_id, "full")
                return Profile()

        store = ProtocolStore(self.base / "provider", protocol=Registry())
        state = store.create_subject("S0002", "1.0", "full", {})
        self.assertEqual(state["expected_condition_ids"], [SECOND_CONDITION_ID])
        store.close()

    def test_protocol_snapshot_is_self_contained_and_registry_independent(self):
        state = self.create_subject([CONDITION_ID, SECOND_CONDITION_ID])
        snapshot = state["protocol_snapshot"]
        self.assertEqual(snapshot["condition_ids"], [CONDITION_ID, SECOND_CONDITION_ID])
        self.assertEqual([item["order"] for item in snapshot["conditions"]], [1, 2])
        self.assertEqual(len(snapshot["measurements"]), 23)
        self.assertEqual(snapshot["capture_policy"]["burst_frame_count"], 5)
        self.assertEqual(snapshot["capture_policy"]["anchor_frame"], "F03")
        self.assertEqual(len(snapshot["sha256"]), 64)
        self.assertEqual(self.store.get_protocol_snapshot("S0001"), snapshot)

        def changed_registry(_profile_id):
            raise AssertionError("old subject recovery must not query the current registry")

        self.reopen_store(protocol=changed_registry)
        self.assertEqual(
            self.store.get_protocol_snapshot("S0001")["condition_ids"],
            [CONDITION_ID, SECOND_CONDITION_ID],
        )

    def test_dataset_lease_rejects_second_instance_without_touching_pending(self):
        self.create_subject()
        attempt_id = self.store.begin_capture_attempt("S0001", condition())
        with self.assertRaisesRegex(ProtocolStoreError, "已有采集实例"):
            ProtocolStore(self.base, dataset_phase="pilot")
        state = self.store.get_subject_state("S0001")
        self.assertEqual(state["attempts"][attempt_id]["status"], "PENDING")

    def test_startup_recovery_aborts_pending_attempt_without_durable_data(self):
        self.create_subject()
        attempt_id = self.store.begin_capture_attempt("S0001", condition())
        self.reopen_store()
        state = self.store.get_subject_state("S0001")
        self.assertEqual(state["attempts"][attempt_id]["status"], "ABORTED")
        self.assertEqual(state["conditions"][CONDITION_ID]["status"], "NEEDS_RETAKE")
        self.assertEqual(self.store.startup_recovery_report["aborted_attempts"], 1)

    def test_startup_recovery_promotes_final_commit_left_with_pending_state(self):
        self.create_subject()
        committed = self.commit_pass(include_ir=False)
        state = self.raw_state()
        attempt = state["attempts"][committed["attempt_id"]]
        attempt.update(
            {
                "status": "PENDING",
                "committed_at": None,
                "quality_status": None,
                "files": [],
                "frames": [],
            }
        )
        state["conditions"][CONDITION_ID]["status"] = "IN_PROGRESS"
        state["conditions"][CONDITION_ID]["accepted_attempt_id"] = None
        self.write_raw_state(state)
        self.reopen_store()
        recovered = self.store.get_subject_state("S0001")
        self.assertEqual(recovered["attempts"][committed["attempt_id"]]["status"], "COMMITTED")
        self.assertEqual(recovered["conditions"][CONDITION_ID]["status"], "CAPTURED")
        self.assertEqual(self.store.startup_recovery_report["recovered_commits"], 1)

    def test_startup_recovery_moves_complete_staging_tree_to_final(self):
        self.create_subject()
        committed = self.commit_pass(include_ir=False)
        first_file = self.base / committed["files"][0]["path"]
        final_dir = first_file.parent.parent
        staging_dir = self.base / "subjects" / "S0001" / ".staging" / committed["attempt_id"]
        final_dir.replace(staging_dir)
        state = self.raw_state()
        state["attempts"][committed["attempt_id"]].update(
            {"status": "PENDING", "quality_status": None, "files": [], "frames": []}
        )
        state["conditions"][CONDITION_ID].update(
            {"status": "IN_PROGRESS", "accepted_attempt_id": None}
        )
        self.write_raw_state(state)
        self.reopen_store()
        recovered = self.store.get_subject_state("S0001")
        self.assertFalse(staging_dir.exists())
        self.assertEqual(
            recovered["conditions"][CONDITION_ID]["accepted_attempt_id"],
            committed["attempt_id"],
        )

    def test_startup_recovery_imports_orphan_anthropometry_without_resigning_known_hash(self):
        self.create_subject()
        saved = self.store.save_anthropometry("S0001", core_anthropometry())
        state = self.raw_state()
        original_sha = state["anthropometry"]["latest_sha256"]
        state["anthropometry"] = {
            "status": "MISSING",
            "revision_count": 0,
            "latest_revision": None,
            "latest_path": None,
        }
        self.write_raw_state(state)
        self.reopen_store()
        recovered = self.raw_state()
        self.assertEqual(recovered["anthropometry"]["latest_revision"], saved["revision"])
        self.assertEqual(recovered["anthropometry"]["latest_sha256"], original_sha)

        # A state-known revision with a mismatching hash is reported, never
        # silently re-signed to the tampered bytes.
        anthro_path = self.base / "subjects" / "S0001" / recovered["anthropometry"]["latest_path"]
        tampered = json.loads(anthro_path.read_text("utf-8"))
        tampered["metadata"]["tampered"] = True
        anthro_path.write_text(json.dumps(tampered), encoding="utf-8")
        known_sha = recovered["anthropometry"]["latest_sha256"]
        report = self.store.recover("S0001", verify_committed=True)
        after = self.raw_state()
        self.assertEqual(after["anthropometry"]["latest_sha256"], known_sha)
        self.assertTrue(
            any("CORRUPTED" in error for error in report["subjects"][0]["audit_errors"])
        )

    def test_commit_writes_five_verified_frames_and_sha256_manifest(self):
        self.create_subject()
        attempt_id = self.store.begin_capture_attempt("S0001", condition(note="turntable mark"))
        committed = self.store.commit_capture_attempt(
            "S0001",
            condition(attempt_id=attempt_id),
            burst(include_ir=True),
            qc={"status": "PASS"},
            camera_metadata={"rgb_color_order": "RGB", "serial": "SERIAL"},
        )
        self.assertEqual(committed["attempt_id"], attempt_id)
        self.assertEqual(committed["status"], "COMMITTED")
        self.assertEqual(committed["disposition"], "PRIMARY")
        self.assertEqual(committed["anchor_frame"], "F03")
        self.assertEqual(len(committed["frames"]), 5)
        self.assertEqual(len(committed["files"]), 30)
        self.assertEqual(
            {entry["modality"] for entry in committed["files"]},
            {
                "rgb", "depth_raw", "depth_aligned", "depth_raw_npy",
                "depth_aligned_npy", "ir",
            },
        )
        for entry in committed["files"]:
            path = self.base / Path(entry["path"])
            self.assertTrue(path.is_file(), path)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"])
            if path.suffix == ".npy":
                array = np.load(path, allow_pickle=False)
                self.assertEqual(array.dtype, np.uint16)
                self.assertEqual(entry["shape"], list(array.shape))
                self.assertGreater(entry["depth_scale_mm_per_unit"], 0)
            else:
                self.assertIsNotNone(
                    cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
                )
            self.assertIn(f"S0001_{CONDITION_ID}_F", path.name)
        for frame_index in range(1, 6):
            for modality in ("depth_raw", "depth_aligned"):
                png_record = next(
                    item for item in committed["files"]
                    if item["frame_index"] == frame_index and item["modality"] == modality
                )
                npy_record = next(
                    item for item in committed["files"]
                    if item["frame_index"] == frame_index and item["modality"] == f"{modality}_npy"
                )
                png = cv2.imdecode(
                    np.frombuffer((self.base / png_record["path"]).read_bytes(), dtype=np.uint8),
                    cv2.IMREAD_UNCHANGED,
                )
                np.testing.assert_array_equal(
                    png, np.load(self.base / npy_record["path"], allow_pickle=False)
                )
        attempt_dir = (self.base / committed["files"][0]["path"]).parent.parent
        self.assertTrue((attempt_dir / "commit.json").is_file())
        state = self.store.get_subject_state("S0001")
        self.assertEqual(state["conditions"][CONDITION_ID]["status"], "CAPTURED")
        manifest_path = self.base / "manifests" / "S0001.jsonl"
        events = [json.loads(line) for line in manifest_path.read_text("utf-8").splitlines()]
        self.assertEqual(
            [event["event"] for event in events],
            ["SUBJECT_CREATED", "CAPTURE_ATTEMPT_BEGUN", "CAPTURE_ATTEMPT_COMMITTED"],
        )
        self.assertEqual(len(events[-1]["files"]), 30)

    def test_bad_capture_and_retake_are_both_preserved(self):
        self.create_subject()
        self.store.begin_capture_attempt("S0001", condition())
        bad = self.store.commit_capture_attempt(
            "S0001",
            condition(),
            burst(include_ir=False),
            qc={"status": "FAIL", "wrong_pose": "FAIL"},
            camera_metadata={},
        )
        self.assertEqual(bad["disposition"], "BAD")
        self.assertTrue(all("_BAD" not in Path(item["path"]).name for item in bad["files"]))
        self.assertEqual(
            self.store.get_subject_state("S0001")["conditions"][CONDITION_ID]["status"],
            "NEEDS_RETAKE",
        )

        self.store.begin_capture_attempt("S0001", condition())
        retake = self.store.commit_capture_attempt(
            "S0001",
            condition(),
            burst(include_ir=False),
            qc={"status": "PASS"},
            camera_metadata={},
        )
        self.assertEqual(retake["disposition"], "RETAKE")
        self.assertTrue(all("_RETAKE" not in Path(item["path"]).name for item in retake["files"]))
        for item in bad["files"] + retake["files"]:
            parse_capture_stem(Path(item["path"]).name)
        for item in bad["files"] + retake["files"]:
            self.assertTrue((self.base / item["path"]).exists())
        state = self.store.get_subject_state("S0001")
        self.assertEqual(len(state["conditions"][CONDITION_ID]["attempt_ids"]), 2)
        self.assertEqual(state["conditions"][CONDITION_ID]["accepted_attempt_id"], retake["attempt_id"])

    def test_failed_manual_retake_preserves_prior_accepted_attempt(self):
        self.create_subject()
        accepted = self.commit_pass(include_ir=False)

        self.store.begin_capture_attempt(
            "S0001",
            condition(
                retake_reason="operator requested a conservative recheck",
                target_attempt_id=accepted["attempt_id"],
                invalidate_prior=False,
            ),
        )
        failed_retake = self.store.commit_capture_attempt(
            "S0001",
            condition(),
            burst(include_ir=False),
            qc={"status": "FAIL", "operator_review": "FAIL"},
            camera_metadata={},
        )

        state = self.store.get_subject_state("S0001")
        condition_state = state["conditions"][CONDITION_ID]
        self.assertEqual(failed_retake["disposition"], "BAD")
        self.assertEqual(condition_state["status"], "CAPTURED")
        self.assertEqual(condition_state["accepted_attempt_id"], accepted["attempt_id"])
        self.assertEqual(len(condition_state["attempt_ids"]), 2)

    def test_write_failure_is_recorded_and_staging_is_not_committed(self):
        self.create_subject()
        attempt_id = self.store.begin_capture_attempt("S0001", condition())
        with mock.patch("backend.core.protocol_store.cv2.imencode", return_value=(False, None)):
            with self.assertRaises(OSError):
                self.store.commit_capture_attempt(
                    "S0001",
                    condition(),
                    burst(include_ir=False),
                    qc={"status": "PASS"},
                    camera_metadata={},
                )
        state = self.store.get_subject_state("S0001")
        self.assertEqual(state["attempts"][attempt_id]["status"], "WRITE_FAILED")
        self.assertEqual(state["conditions"][CONDITION_ID]["status"], "NEEDS_RETAKE")
        final = (
            self.base
            / "subjects"
            / "S0001"
            / "cameras"
            / "C336L"
            / "conditions"
            / CONDITION_ID
            / "attempts"
            / attempt_id
        )
        self.assertFalse(final.exists())
        staging = list((self.base / "subjects" / "S0001" / ".staging").iterdir())
        self.assertEqual(len(staging), 1)
        self.assertEqual(staging[0].name, attempt_id)
        # The failed reservation no longer blocks a fresh retake.
        self.assertIsInstance(self.store.begin_capture_attempt("S0001", condition()), str)

    def test_commit_retries_transient_windows_staging_promotion_lock(self):
        self.create_subject()
        self.store.begin_capture_attempt("S0001", condition())
        real_replace = protocol_store_module.os.replace
        transient_failures = 0

        def flaky_replace(source, destination):
            nonlocal transient_failures
            source_path = Path(source)
            if (
                source_path.is_dir()
                and source_path.parent.name == ".staging"
                and transient_failures < 2
            ):
                transient_failures += 1
                error = PermissionError(
                    13, "simulated transient Windows directory lock"
                )
                error.winerror = 5
                raise error
            return real_replace(source, destination)

        with (
            mock.patch(
                "backend.core.protocol_store.os.replace",
                side_effect=flaky_replace,
            ),
            mock.patch("backend.core.protocol_store.time.sleep") as sleep_mock,
            mock.patch("backend.core.protocol_store.os.name", "nt"),
        ):
            committed = self.store.commit_capture_attempt(
                "S0001",
                condition(),
                burst(include_ir=False),
                qc={"status": "PASS"},
                camera_metadata={},
            )

        self.assertEqual(transient_failures, 2)
        self.assertEqual(sleep_mock.call_count, 2)
        self.assertEqual(committed["status"], "COMMITTED")
        first_file = self.base / committed["files"][0]["path"]
        self.assertTrue(first_file.is_file())
        self.assertFalse(
            (
                self.base
                / "subjects"
                / "S0001"
                / ".staging"
                / committed["attempt_id"]
            ).exists()
        )

    def test_exhausted_windows_promotion_retries_preserve_staging(self):
        self.create_subject()
        attempt_id = self.store.begin_capture_attempt("S0001", condition())
        real_replace = protocol_store_module.os.replace
        promotion_calls = 0

        def locked_replace(source, destination):
            nonlocal promotion_calls
            source_path = Path(source)
            if source_path.is_dir() and source_path.parent.name == ".staging":
                promotion_calls += 1
                error = PermissionError(13, "simulated persistent Windows lock")
                error.winerror = 32
                raise error
            return real_replace(source, destination)

        with (
            mock.patch(
                "backend.core.protocol_store.os.replace",
                side_effect=locked_replace,
            ),
            mock.patch("backend.core.protocol_store.time.sleep") as sleep_mock,
            mock.patch("backend.core.protocol_store.os.name", "nt"),
            self.assertRaises(PermissionError),
        ):
            self.store.commit_capture_attempt(
                "S0001",
                condition(),
                burst(include_ir=False),
                qc={"status": "PASS"},
                camera_metadata={},
            )

        self.assertEqual(promotion_calls, 6)
        self.assertEqual(sleep_mock.call_count, 5)
        staging = self.base / "subjects" / "S0001" / ".staging" / attempt_id
        self.assertTrue((staging / "commit.json").is_file())
        final = (
            self.base
            / "subjects"
            / "S0001"
            / "cameras"
            / "C336L"
            / "conditions"
            / CONDITION_ID
            / "attempts"
            / attempt_id
        )
        self.assertFalse(final.exists())
        state = self.store.get_subject_state("S0001")
        self.assertEqual(state["attempts"][attempt_id]["status"], "WRITE_FAILED")

    def test_atomic_file_replace_retries_transient_windows_lock(self):
        destination = self.base / "atomic.json"
        self.store._atomic_write_json(destination, {"revision": 1})
        real_replace = protocol_store_module.os.replace
        transient_failures = 0

        def flaky_replace(source, target):
            nonlocal transient_failures
            if Path(target) == destination and transient_failures < 2:
                transient_failures += 1
                error = PermissionError(13, "simulated manifest sharing violation")
                error.winerror = 32
                raise error
            return real_replace(source, target)

        with (
            mock.patch(
                "backend.core.protocol_store.os.replace",
                side_effect=flaky_replace,
            ),
            mock.patch("backend.core.protocol_store.time.sleep") as sleep_mock,
            mock.patch("backend.core.protocol_store.os.name", "nt"),
        ):
            self.store._atomic_write_json(destination, {"revision": 2})

        self.assertEqual(transient_failures, 2)
        self.assertEqual(sleep_mock.call_count, 2)
        self.assertEqual(json.loads(destination.read_text("utf-8")), {"revision": 2})

    def test_pending_attempt_can_be_explicitly_aborted(self):
        self.create_subject()
        attempt_id = self.store.begin_capture_attempt("S0001", condition())
        aborted = self.store.fail_capture_attempt(
            "S0001", condition(attempt_id=attempt_id), "operator cancelled before burst"
        )
        self.assertEqual(aborted["status"], "ABORTED")
        self.assertEqual(aborted["error"], "operator cancelled before burst")
        self.assertEqual(
            self.store.get_subject_state("S0001")["conditions"][CONDITION_ID]["status"],
            "NEEDS_RETAKE",
        )
        self.assertIsInstance(self.store.begin_capture_attempt("S0001", condition()), str)

    def test_burst_validation_rejects_wrong_count_and_missing_raw_depth(self):
        self.create_subject()
        self.store.begin_capture_attempt("S0001", condition())
        with self.assertRaises(ProtocolValidationError):
            self.store.commit_capture_attempt(
                "S0001", condition(), burst()[:4], {"status": "PASS"}, {}
            )
        failed_state = self.store.get_subject_state("S0001")
        self.assertEqual(
            next(iter(failed_state["attempts"].values()))["status"], "WRITE_FAILED"
        )
        self.store.begin_capture_attempt("S0001", condition())
        missing = burst()
        del missing[2]["depth_raw"]
        with self.assertRaises(ProtocolValidationError):
            self.store.commit_capture_attempt(
                "S0001", condition(), missing, {"status": "PASS"}, {}
            )

    def test_transposed_burst_and_stereo_ir_are_supported(self):
        self.create_subject()
        items = burst(stereo_ir=True)
        transposed = {
            key: np.stack([frame[key] for frame in items])
            for key in ("rgb", "depth_raw", "depth_aligned", "ir_left", "ir_right")
        }
        self.store.begin_capture_attempt("S0001", condition())
        committed = self.store.commit_capture_attempt(
            "S0001", condition(), transposed, {"status": "WARN"}, {}
        )
        self.assertEqual(committed["quality_status"], "WARN")
        self.assertEqual(
            self.store.get_subject_state("S0001")["conditions"][CONDITION_ID]["status"],
            "REVIEW_REQUIRED",
        )
        self.assertEqual(len(committed["files"]), 35)
        self.assertEqual(
            sum(item["modality"] == "ir_left" for item in committed["files"]), 5
        )
        self.assertEqual(
            sum(item["modality"] == "ir_right" for item in committed["files"]), 5
        )

    def test_warn_requires_manual_review_accept_and_reject(self):
        self.create_subject()
        self.store.begin_capture_attempt("S0001", condition())
        warned = self.store.commit_capture_attempt(
            "S0001",
            condition(),
            burst(include_ir=False),
            {"status": "REVIEW_REQUIRED", "operator_question": "WARN"},
            {},
        )
        state = self.store.get_subject_state("S0001")
        self.assertEqual(warned["review_status"], "PENDING")
        self.assertEqual(state["conditions"][CONDITION_ID]["status"], "REVIEW_REQUIRED")
        with self.assertRaises(ProtocolValidationError):
            self.store.begin_capture_attempt("S0001", condition())
        with self.assertRaises(ProtocolStoreError):
            self.store.review_capture_attempt(
                "S0001", condition(), warned["attempt_id"], "MAYBE", "RV01", "invalid"
            )

        review = self.store.review_capture_attempt(
            "S0001",
            condition(),
            warned["attempt_id"],
            "ACCEPT",
            "RV01",
            "人体完整，轻微亮度警告不影响形态",
            {"policy_version": "manual-v1"},
        )
        self.assertEqual(review["review_status"], "ACCEPTED")
        accepted_state = self.store.get_subject_state("S0001")
        self.assertEqual(accepted_state["conditions"][CONDITION_ID]["status"], "CAPTURED")
        self.assertEqual(
            accepted_state["conditions"][CONDITION_ID]["accepted_attempt_id"],
            warned["attempt_id"],
        )
        self.store.save_anthropometry("S0001", core_anthropometry())
        self.assertEqual(self.store.complete_subject("S0001")["status"], "COMPLETE")
        with self.assertRaises(ProtocolStoreError):
            self.store.review_capture_attempt(
                "S0001", condition(), warned["attempt_id"], "REJECT", "RV02", "second review"
            )

    def test_startup_recovery_replays_durable_warn_review(self):
        self.create_subject()
        self.store.begin_capture_attempt("S0001", condition())
        warned = self.store.commit_capture_attempt(
            "S0001", condition(), burst(include_ir=False), {"status": "WARN"}, {}
        )
        review = self.store.review_capture_attempt(
            "S0001", condition(), warned["attempt_id"], "ACCEPT", "RV01", "manual pass"
        )
        state = self.raw_state()
        state["attempts"][warned["attempt_id"]]["review_status"] = "PENDING"
        state["attempts"][warned["attempt_id"]]["review"] = None
        state["conditions"][CONDITION_ID].update(
            {"status": "REVIEW_REQUIRED", "accepted_attempt_id": None}
        )
        self.write_raw_state(state)
        self.reopen_store()
        recovered = self.store.get_subject_state("S0001")
        self.assertEqual(
            recovered["attempts"][warned["attempt_id"]]["review_status"],
            "ACCEPTED",
        )
        self.assertEqual(
            recovered["attempts"][warned["attempt_id"]]["review"]["review_id"],
            review["review_id"],
        )
        self.assertEqual(recovered["conditions"][CONDITION_ID]["status"], "CAPTURED")

    def test_rejected_warn_requires_retake(self):
        self.create_subject()
        self.store.begin_capture_attempt("S0001", condition())
        warned = self.store.commit_capture_attempt(
            "S0001", condition(), burst(include_ir=False), {"status": "WARN"}, {}
        )
        rejected = self.store.review_capture_attempt(
            "S0001",
            condition(),
            warned["attempt_id"],
            "REJECT",
            "RV01",
            "脚部出框",
        )
        self.assertEqual(rejected["review_status"], "REJECTED")
        self.assertEqual(
            self.store.get_subject_state("S0001")["conditions"][CONDITION_ID]["status"],
            "NEEDS_RETAKE",
        )
        self.assertIsInstance(self.store.begin_capture_attempt("S0001", condition()), str)

    def test_explicit_retake_invalidation_and_supersession(self):
        self.create_subject()
        original = self.commit_pass(include_ir=False)
        with self.assertRaises(ProtocolValidationError):
            self.store.begin_capture_attempt("S0001", condition())
        with self.assertRaises(ProtocolValidationError):
            self.store.begin_capture_attempt(
                "S0001",
                condition(
                    retake_reason="wrong target",
                    target_attempt_id="not-current",
                    invalidate_prior=False,
                ),
            )

        self.store.begin_capture_attempt(
            "S0001",
            condition(
                retake_reason="发现原采集服装标签错误，旧数据立即失效",
                target_attempt_id=original["attempt_id"],
                invalidate_prior=True,
            ),
        )
        during = self.store.get_subject_state("S0001")
        self.assertEqual(during["attempts"][original["attempt_id"]]["validity"], "INVALIDATED")
        self.assertIsNone(during["conditions"][CONDITION_ID]["accepted_attempt_id"])
        replacement = self.store.commit_capture_attempt(
            "S0001", condition(), burst(include_ir=False), {"status": "PASS"}, {}
        )
        self.assertEqual(replacement["supersedes_attempt_id"], original["attempt_id"])
        final = self.store.get_subject_state("S0001")
        self.assertEqual(
            final["conditions"][CONDITION_ID]["accepted_attempt_id"],
            replacement["attempt_id"],
        )

    def test_anthropometry_requires_repeats_and_calculates_closest_pair(self):
        self.create_subject()
        missing = core_anthropometry()
        del missing["M12"]
        with self.assertRaisesRegex(ProtocolValidationError, "M12"):
            self.store.save_anthropometry("S0001", missing)

        needs_third = core_anthropometry()
        needs_third["M01"] = [170.0, 171.0]
        with self.assertRaisesRegex(ProtocolValidationError, "measurement_3"):
            self.store.save_anthropometry("S0001", needs_third)

        values = core_anthropometry()
        values["M01"] = [170.0, 171.0, 170.2]
        values["M04"] = [80.0, 81.5, 80.4]
        record = self.store.save_anthropometry("S0001", values)
        self.assertEqual(record["measurements"]["M01"]["final_value"], 170.1)
        self.assertEqual(
            record["measurements"]["M01"]["final_source_measurements"], [1, 3]
        )
        # M02's large discrepancy remains valid with two readings.
        self.assertEqual(record["measurements"]["M02"]["final_value"], 67.5)
        self.assertTrue(any(item["measurement_id"] == "M02" for item in record["warnings"]))
        self.assertTrue(all(item["operator_id"] == "OP01" for item in record["records"]))
        self.assertTrue(all(item["recorded_at"] for item in record["records"]))
        self.assertIsNone(record["measurements"]["M14"])
        first_path = self.base / "subjects" / "S0001" / "meta" / "anthropometry" / "anthropometry_0001.json"
        self.assertTrue(first_path.exists())
        second = self.store.save_anthropometry("S0001", core_anthropometry())
        self.assertEqual(second["revision"], 2)
        self.assertTrue(first_path.exists())
        latest = self.store.get_latest_anthropometry("S0001")
        self.assertEqual(latest["revision"], 2)
        reloaded = self.store.get_subject_state("S0001")
        self.assertTrue(reloaded["anthropometry"]["complete"])
        self.assertEqual(len(reloaded["anthropometry"]["records"]), 6)

    def test_frontend_records_and_bilateral_optional_fields_round_trip(self):
        self.create_subject()
        records = []
        for measurement_id in CORE_MEASUREMENT_IDS:
            field_name = MEASUREMENTS_BY_ID[measurement_id].field_names[0]
            base_value = 170.0 if measurement_id == "M01" else 40.0 if measurement_id == "M03" else 70.0
            records.append(
                {
                    "measurement_id": measurement_id,
                    "field_name": field_name,
                    "m1": base_value,
                    "m2": base_value + 0.2,
                }
            )
        m16_fields = MEASUREMENTS_BY_ID["M16"].field_names
        for offset, field_name in enumerate(m16_fields):
            records.append(
                {
                    "measurement_id": "M16",
                    "field_name": field_name,
                    "m1": 30.0 + offset,
                    "m2": 30.4 + offset,
                }
            )
        saved = self.store.save_anthropometry("S0001", records)
        self.assertEqual(set(saved["measurements"]["M16"]), set(m16_fields))
        self.assertEqual(len(saved["records"]), 7)
        state_records = self.store.get_subject_state("S0001")["anthropometry"]["records"]
        self.assertEqual(
            {item["field_name"] for item in state_records if item["measurement_id"] == "M16"},
            set(m16_fields),
        )

        partial = core_anthropometry()
        partial["M16"] = {m16_fields[0]: [30.0, 30.2]}
        with self.assertRaisesRegex(ProtocolValidationError, "partially filled"):
            self.store.save_anthropometry("S0001", partial)

    def test_completion_gate_and_post_completion_write_lock(self):
        self.create_subject()
        self.commit_pass(include_ir=False)
        report = self.store.completion_report("S0001")
        self.assertEqual(report["captured_conditions"], 1)
        self.assertFalse(report["anthropometry_complete"])
        with self.assertRaises(IncompleteSubjectError):
            self.store.complete_subject("S0001")

        self.store.save_anthropometry("S0001", core_anthropometry())
        complete = self.store.complete_subject("S0001")
        self.assertEqual(complete["status"], "COMPLETE")
        self.assertTrue(complete["ready_to_complete"])
        self.assertEqual(complete["missing"], 0)
        self.assertEqual(complete["anthro_completed"], 5)
        with self.assertRaises(SubjectCompletedError):
            self.store.begin_capture_attempt("S0001", condition())
        with self.assertRaises(SubjectCompletedError):
            self.store.save_anthropometry("S0001", core_anthropometry())
        # Closure is idempotent and does not mutate the frozen report.
        self.assertEqual(self.store.complete_subject("S0001"), complete)

    def test_missing_second_condition_blocks_completion(self):
        self.create_subject([CONDITION_ID, SECOND_CONDITION_ID])
        self.commit_pass(include_ir=False)
        self.store.save_anthropometry("S0001", core_anthropometry())
        with self.assertRaises(IncompleteSubjectError) as caught:
            self.store.complete_subject("S0001")
        self.assertEqual(caught.exception.report["missing_condition_ids"], [SECOND_CONDITION_ID])

    def test_completion_rechecks_file_hashes_and_detects_post_completion_loss(self):
        self.create_subject()
        accepted = self.commit_pass(include_ir=False)
        self.store.save_anthropometry("S0001", core_anthropometry())
        complete = self.store.complete_subject("S0001")
        self.assertTrue(complete["ready_to_complete"])

        rgb = next(item for item in accepted["files"] if item["modality"] == "rgb")
        (self.base / rgb["path"]).unlink()
        audited = self.store.completion_report("S0001")
        self.assertEqual(audited["status"], "CORRUPTED")
        self.assertFalse(audited["ready_to_complete"])
        self.assertIn(CONDITION_ID, audited["invalid_condition_ids"])
        self.assertTrue(any("missing file" in item for item in audited["integrity_errors"]))

    def test_strict_frozen_contract_modalities_qc_and_equipment(self):
        self.create_strict_subject()

        self.store.begin_capture_attempt("S0001", condition())
        with self.assertRaisesRegex(ProtocolValidationError, "ir_right"):
            self.store.commit_capture_attempt(
                "S0001",
                condition(),
                burst(include_ir=True, stereo_ir=False),
                {"status": "PASS"},
                {},
            )

        self.store.begin_capture_attempt("S0001", condition())
        with self.assertRaisesRegex(ProtocolValidationError, "policy_version"):
            self.store.commit_capture_attempt(
                "S0001",
                condition(),
                burst(include_ir=True, stereo_ir=True),
                {"status": "PASS"},
                {},
            )

        self.store.begin_capture_attempt("S0001", condition())
        qc = strict_qc_payload()
        forged_qc = dict(qc)
        forged_qc["checks"] = [
            {
                "code": "HUMAN_CONTENT_MANUAL_REVIEW",
                "status": "WARN",
                "frame": "F03",
            }
        ]
        with self.assertRaisesRegex(ProtocolValidationError, "count differs"):
            self.store.commit_capture_attempt(
                "S0001",
                condition(),
                burst(include_ir=True, stereo_ir=True),
                forged_qc,
                strict_camera_metadata(forged_qc),
            )

        self.store.begin_capture_attempt("S0001", condition())
        committed = self.store.commit_capture_attempt(
            "S0001",
            condition(),
            burst(include_ir=True, stereo_ir=True),
            qc,
            strict_camera_metadata(qc),
        )
        self.assertEqual(committed["quality_status"], "WARN")
        reviewed = self.store.review_capture_attempt(
            "S0001",
            condition(),
            committed["attempt_id"],
            "ACCEPT",
            "RV01",
            "已核对 F03 人体、姿态和标签",
        )
        self.assertEqual(reviewed["review_status"], "ACCEPTED")

        with self.assertRaisesRegex(ProtocolValidationError, "metadata.equipment"):
            self.store.save_anthropometry("S0001", core_anthropometry())
        invalid = core_anthropometry()
        invalid["M01"] = [70.0, 70.1]
        with self.assertRaisesRegex(ProtocolValidationError, "hard range"):
            self.store.save_anthropometry(
                "S0001", invalid, anthropometry_equipment()
            )
        saved = self.store.save_anthropometry(
            "S0001", core_anthropometry(), anthropometry_equipment()
        )
        self.assertEqual(
            saved["metadata"]["equipment"]["scale_id"], "SCALE01"
        )
        reloaded_anthro = self.store.get_subject_state("S0001")["anthropometry"]
        self.assertEqual(
            reloaded_anthro["metadata"]["equipment"]["tape_id"], "TAPE01"
        )
        self.assertEqual(reloaded_anthro["metadata"]["operator_id"], "OP01")
        completed = self.store.complete_subject("S0001")
        self.assertEqual(completed["status"], "COMPLETE")
        self.assertTrue(completed["manifest_head_sha256"])
        self.assertTrue(completed["content_sha256"])

    def test_sidecars_and_manifest_hash_chain_are_completion_evidence(self):
        self.create_subject()
        accepted = self.commit_pass(include_ir=False)
        self.store.save_anthropometry("S0001", core_anthropometry())
        self.assertEqual(
            set(accepted["sidecars"]), {"capture.json", "qc.json", "commit.json"}
        )
        manifest_path = self.base / "manifests" / "S0001.jsonl"
        events = [json.loads(line) for line in manifest_path.read_text("utf-8").splitlines()]
        self.assertEqual(
            [item["event_index"] for item in events], list(range(1, len(events) + 1))
        )
        self.assertTrue(all(item["event_sha256"] for item in events))

        attempt_file = self.base / accepted["files"][0]["path"]
        qc_path = attempt_file.parent.parent / "qc.json"
        qc_path.write_text('{"status":"FAIL"}\n', encoding="utf-8")
        report = self.store.completion_report("S0001")
        self.assertFalse(report["ready_to_complete"])
        self.assertTrue(
            any("sidecar integrity failed" in item for item in report["integrity_errors"])
        )

    def test_manifest_and_completion_report_tampering_is_not_silently_trusted(self):
        self.create_subject()
        self.commit_pass(include_ir=False)
        self.store.save_anthropometry("S0001", core_anthropometry())
        completed = self.store.complete_subject("S0001")
        report_path = (
            self.base
            / "subjects"
            / "S0001"
            / "meta"
            / "subject_completion_report.json"
        )
        tampered_report = json.loads(report_path.read_text("utf-8"))
        tampered_report["captured_conditions"] = 999
        report_path.write_text(json.dumps(tampered_report), encoding="utf-8")
        repaired = self.store.completion_report("S0001")
        self.assertEqual(repaired["captured_conditions"], 1)
        self.assertNotEqual(repaired["content_sha256"], completed["content_sha256"])

        manifest_path = self.base / "manifests" / "S0001.jsonl"
        lines = manifest_path.read_text("utf-8").splitlines()
        first = json.loads(lines[0])
        first["profile_id"] = "tampered"
        lines[0] = json.dumps(first, ensure_ascii=False, sort_keys=True)
        manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        corrupted = self.store.completion_report("S0001")
        self.assertEqual(corrupted["status"], "CORRUPTED")
        self.assertTrue(any("manifest" in item for item in corrupted["integrity_errors"]))

    def test_observed_stream_shape_and_dtype_are_enforced(self):
        self.create_subject()
        self.store.begin_capture_attempt("S0001", condition())
        with self.assertRaisesRegex(ProtocolValidationError, "observed_streams"):
            self.store.commit_capture_attempt(
                "S0001",
                condition(),
                burst(include_ir=False),
                {"status": "PASS"},
                {
                    "observed_streams": {
                        "color": {"shape": [8, 10, 3], "dtype": "uint8"},
                        "depth_raw": {"shape": [6, 7], "dtype": "uint8"},
                        "depth_aligned": {
                            "shape": [8, 10],
                            "dtype": "uint16",
                        },
                    }
                },
            )

    def test_subject_camera_fingerprint_rejects_device_change(self):
        self.store.create_subject(
            "S0001",
            protocol_version="1.0",
            profile_id="camera_lock",
            subject_metadata={"operator_id": "OP01"},
            expected_conditions=[CONDITION_ID, SECOND_GEMINI_CONDITION_ID],
            capture_policy={"lock_camera_fingerprint": True},
        )

        def camera_metadata(serial):
            return {
                "camera_serial": serial,
                "calibration_sha256": "a" * 64,
                "depth_scale_mm_per_unit": 1.0,
                "stream_profiles": {
                    "color": {
                        "width": 10,
                        "height": 8,
                        "fps": 30,
                        "format": "RGB8",
                    },
                    "depth_raw": {
                        "width": 7,
                        "height": 6,
                        "fps": 30,
                        "format": "Y16",
                    },
                },
            }

        self.store.begin_capture_attempt("S0001", condition())
        first = self.store.commit_capture_attempt(
            "S0001",
            condition(),
            burst(include_ir=False),
            {"status": "PASS", "body_full_visible": "PASS"},
            camera_metadata("SERIAL_A"),
        )
        self.assertTrue(first["camera_metadata"]["subject_camera_fingerprint_sha256"])

        self.store.begin_capture_attempt(
            "S0001",
            condition(SECOND_GEMINI_CONDITION_ID),
        )
        with self.assertRaisesRegex(
            ProtocolValidationError,
            "camera fingerprint changed",
        ):
            self.store.commit_capture_attempt(
                "S0001",
                condition(SECOND_GEMINI_CONDITION_ID),
                burst(include_ir=False),
                {"status": "PASS", "body_full_visible": "PASS"},
                camera_metadata("SERIAL_B"),
            )

    def test_recovery_keeps_unreviewed_warn_as_review_required(self):
        self.create_subject()
        original = self.commit_pass(include_ir=False)
        self.store.begin_capture_attempt(
            "S0001",
            condition(
                retake_reason="补拍对比，暂不废弃原通过数据",
                target_attempt_id=original["attempt_id"],
                invalidate_prior=False,
            ),
        )
        warned = self.store.commit_capture_attempt(
            "S0001",
            condition(),
            burst(include_ir=False),
            {"status": "WARN"},
            {},
        )
        raw = self.raw_state()
        raw["conditions"][CONDITION_ID]["status"] = "CAPTURED"
        self.write_raw_state(raw)
        self.reopen_store()
        recovered = self.store.get_subject_state("S0001")
        self.assertEqual(
            recovered["conditions"][CONDITION_ID]["status"], "REVIEW_REQUIRED"
        )
        self.assertEqual(
            recovered["conditions"][CONDITION_ID]["accepted_attempt_id"],
            original["attempt_id"],
        )
        self.assertEqual(
            recovered["attempts"][warned["attempt_id"]]["review_status"], "PENDING"
        )
        with self.assertRaisesRegex(ProtocolValidationError, "awaiting manual review"):
            self.store.begin_capture_attempt("S0001", condition())

    def test_rejected_review_cannot_be_forged_accepted_in_subject_state(self):
        self.create_subject()
        self.store.begin_capture_attempt("S0001", condition())
        warned = self.store.commit_capture_attempt(
            "S0001",
            condition(),
            burst(include_ir=False),
            {"status": "WARN"},
            {},
        )
        self.store.review_capture_attempt(
            "S0001",
            condition(),
            warned["attempt_id"],
            "REJECT",
            "RV01",
            "真实复核结论为拒绝",
        )
        self.store.save_anthropometry("S0001", core_anthropometry())
        forged = self.raw_state()
        forged_attempt = forged["attempts"][warned["attempt_id"]]
        forged_attempt["review_status"] = "ACCEPTED"
        forged["conditions"][CONDITION_ID]["status"] = "CAPTURED"
        forged["conditions"][CONDITION_ID]["accepted_attempt_id"] = warned[
            "attempt_id"
        ]
        self.write_raw_state(forged)
        report = self.store.completion_report("S0001")
        self.assertFalse(report["ready_to_complete"])
        self.assertTrue(
            any(
                "not an ACCEPT decision" in item
                or "state review differs" in item
                for item in report["integrity_errors"]
            )
        )

    def test_post_rename_bookkeeping_failure_recovers_every_failed_state(self):
        self.create_subject()
        self.store.begin_capture_attempt("S0001", condition())
        original_append = self.store._atomic_append_jsonl

        def fail_committed_manifest(path, value):
            if value.get("event") == "CAPTURE_ATTEMPT_COMMITTED":
                raise OSError("injected post-rename manifest failure")
            return original_append(path, value)

        with mock.patch.object(
            self.store, "_atomic_append_jsonl", side_effect=fail_committed_manifest
        ):
            durable = self.store.commit_capture_attempt(
                "S0001",
                condition(),
                burst(include_ir=False),
                {"status": "PASS"},
                {},
            )
        self.assertEqual(durable["bookkeeping_status"], "RECOVERED")
        attempt_id = durable["attempt_id"]
        self.assertEqual(self.raw_state()["attempts"][attempt_id]["status"], "COMMITTED")

        pending = self.raw_state()
        pending["attempts"][attempt_id]["status"] = "PENDING"
        pending["conditions"][CONDITION_ID]["status"] = "IN_PROGRESS"
        pending["conditions"][CONDITION_ID]["accepted_attempt_id"] = None
        self.write_raw_state(pending)

        with self.assertRaisesRegex(ProtocolValidationError, "valid durable final commit"):
            self.store.fail_capture_attempt(
                "S0001", condition(attempt_id=attempt_id), "caller tried to abort"
            )
        self.assertEqual(
            self.store.get_subject_state("S0001")["attempts"][attempt_id]["status"],
            "COMMITTED",
        )

        for failed_status in ("ABORTED", "WRITE_FAILED"):
            raw = self.raw_state()
            raw["attempts"][attempt_id]["status"] = failed_status
            raw["conditions"][CONDITION_ID]["status"] = "NEEDS_RETAKE"
            raw["conditions"][CONDITION_ID]["accepted_attempt_id"] = None
            self.write_raw_state(raw)
            self.reopen_store()
            recovered = self.store.get_subject_state("S0001")
            self.assertEqual(
                recovered["attempts"][attempt_id]["status"], "COMMITTED"
            )
            self.assertEqual(
                recovered["conditions"][CONDITION_ID]["accepted_attempt_id"],
                attempt_id,
            )

    def test_list_subjects_isolates_unreadable_subject_state(self):
        self.create_subject()
        bad_dir = self.base / "subjects" / "S_BAD" / "meta"
        bad_dir.mkdir(parents=True)
        (bad_dir / "subject_state.json").write_text("{broken", encoding="utf-8")
        summaries = {item["subject_id"]: item for item in self.store.list_subjects()}
        self.assertEqual(summaries["S0001"]["status"], "ACTIVE")
        self.assertEqual(summaries["S_BAD"]["status"], "UNREADABLE")
        self.assertIn("error", summaries["S_BAD"])

    def test_verified_anchor_ignores_mutable_paths_and_detects_replacement(self):
        self.create_subject([CONDITION_ID, SECOND_CONDITION_ID])
        accepted = self.commit_pass(include_ir=False)
        evidence = self.store.get_verified_anchor_files(
            "S0001", CONDITION_ID, accepted["attempt_id"]
        )
        self.assertEqual(set(evidence["files"]), {"rgb", "depth_aligned"})
        self.assertTrue(evidence["files"]["rgb"]["bytes"].startswith(b"\x89PNG"))
        self.assertTrue(evidence["evidence_sha256"])
        self.assertEqual(evidence["camera_metadata"]["camera_serial"], "TEST123")
        with self.assertRaisesRegex(ProtocolValidationError, "does not belong"):
            self.store.get_verified_anchor_files(
                "S0001", SECOND_CONDITION_ID, accepted["attempt_id"]
            )

        raw = self.raw_state()
        raw["attempts"][accepted["attempt_id"]]["files"][0]["path"] = (
            "subjects/S0001/meta/protocol_snapshot.json"
        )
        self.write_raw_state(raw)
        with self.assertRaisesRegex(ProtocolStoreError, "state files differs"):
            self.store.get_verified_anchor_files(
                "S0001", CONDITION_ID, accepted["attempt_id"]
            )

        raw["attempts"][accepted["attempt_id"]] = accepted
        self.write_raw_state(raw)
        rgb_path = self.base / evidence["files"]["rgb"]["relative_path"]
        rgb_path.write_bytes(b"not-a-png")
        with self.assertRaisesRegex(ProtocolStoreError, "size mismatch|hash mismatch"):
            self.store.get_verified_anchor_files(
                "S0001", CONDITION_ID, accepted["attempt_id"]
            )

    def test_strict_anthropometry_uses_frozen_measurement_snapshot(self):
        self.create_strict_subject()
        upgraded = dict(protocol_store_module._MEASUREMENT_DEFINITIONS["M01"])
        upgraded["fields"] = ("future_height_field_cm",)
        upgraded["threshold"] = 0.01
        with mock.patch.dict(
            protocol_store_module._MEASUREMENT_DEFINITIONS,
            {"M01": upgraded},
        ):
            saved = self.store.save_anthropometry(
                "S0001",
                core_anthropometry(),
                anthropometry_equipment(),
            )
        m01_record = next(
            item for item in saved["records"] if item["measurement_id"] == "M01"
        )
        self.assertEqual(m01_record["field_name"], "height_cm")
        self.assertEqual(
            saved["measurements"]["M01"]["third_measurement_threshold"], 0.5
        )

    def test_review_and_anthropometry_post_file_failures_reconcile_in_process(self):
        self.create_subject()
        self.store.begin_capture_attempt("S0001", condition())
        warned = self.store.commit_capture_attempt(
            "S0001",
            condition(),
            burst(include_ir=False),
            {"status": "WARN"},
            {},
        )
        original_append = self.store._atomic_append_jsonl

        def fail_review_event(path, value):
            if value.get("event") == "CAPTURE_ATTEMPT_REVIEWED":
                raise OSError("injected review bookkeeping failure")
            return original_append(path, value)

        with mock.patch.object(
            self.store, "_atomic_append_jsonl", side_effect=fail_review_event
        ):
            review = self.store.review_capture_attempt(
                "S0001",
                condition(),
                warned["attempt_id"],
                "ACCEPT",
                "RV01",
                "已查看 F03",
            )
        self.assertEqual(review["bookkeeping_status"], "RECOVERED")
        self.assertEqual(
            self.store.get_subject_state("S0001")["attempts"][warned["attempt_id"]][
                "review_status"
            ],
            "ACCEPTED",
        )
        review_files = list(
            (
                self.base
                / "subjects"
                / "S0001"
                / "meta"
                / "reviews"
                / warned["attempt_id"]
            ).glob("*.json")
        )
        self.assertEqual(len(review_files), 1)

        def fail_anthro_event(path, value):
            if value.get("event") == "ANTHROPOMETRY_SAVED":
                raise OSError("injected anthropometry bookkeeping failure")
            return original_append(path, value)

        with mock.patch.object(
            self.store, "_atomic_append_jsonl", side_effect=fail_anthro_event
        ):
            saved = self.store.save_anthropometry(
                "S0001", core_anthropometry()
            )
        self.assertEqual(saved["bookkeeping_status"], "RECOVERED")
        self.assertTrue(
            self.store.get_subject_state("S0001")["anthropometry"]["complete"]
        )
        second = self.store.save_anthropometry("S0001", core_anthropometry())
        self.assertEqual(second["revision"], 2)

    def test_complete_subject_report_failure_returns_committed_and_repairs(self):
        self.create_subject()
        self.commit_pass(include_ir=False)
        self.store.save_anthropometry("S0001", core_anthropometry())
        original_write = self.store._atomic_write_json

        def fail_final_report(path, value):
            if (
                path.name == "subject_completion_report.json"
                and value.get("status") == "COMPLETE"
            ):
                raise OSError("injected final report failure")
            return original_write(path, value)

        with mock.patch.object(
            self.store, "_atomic_write_json", side_effect=fail_final_report
        ):
            completed = self.store.complete_subject("S0001")
        self.assertEqual(completed["status"], "COMPLETE")
        self.assertEqual(
            completed["bookkeeping_status"], "REPORT_PENDING_REBUILD"
        )
        self.assertEqual(self.raw_state()["status"], "COMPLETE")
        repaired = self.store.completion_report("S0001")
        self.assertEqual(repaired["status"], "COMPLETE")
        self.assertNotIn("bookkeeping_status", repaired)

    def test_recovered_pass_retake_supersedes_prior_accepted_attempt(self):
        self.create_subject()
        original = self.commit_pass(include_ir=False)
        self.store.begin_capture_attempt(
            "S0001",
            condition(
                retake_reason="补拍更清晰版本并保留旧数据审计",
                target_attempt_id=original["attempt_id"],
                invalidate_prior=False,
            ),
        )
        original_append = self.store._atomic_append_jsonl

        def fail_commit_event(path, value):
            if value.get("event") == "CAPTURE_ATTEMPT_COMMITTED":
                raise OSError("injected retake bookkeeping failure")
            return original_append(path, value)

        with mock.patch.object(
            self.store, "_atomic_append_jsonl", side_effect=fail_commit_event
        ):
            replacement = self.store.commit_capture_attempt(
                "S0001",
                condition(),
                burst(include_ir=False),
                {"status": "PASS"},
                {},
            )
        self.assertEqual(replacement["bookkeeping_status"], "RECOVERED")
        self.assertEqual(
            replacement["supersedes_attempt_id"], original["attempt_id"]
        )
        state = self.store.get_subject_state("S0001")
        self.assertEqual(
            state["conditions"][CONDITION_ID]["accepted_attempt_id"],
            replacement["attempt_id"],
        )

    def test_durable_completion_event_replays_and_blocks_all_future_writes(self):
        self.create_subject()
        self.commit_pass(include_ir=False)
        self.store.save_anthropometry("S0001", core_anthropometry())
        original_write = self.store._atomic_write_json
        state_path = self.base / "subjects" / "S0001" / "meta" / "subject_state.json"

        def fail_complete_state(path, value):
            if path == state_path and value.get("status") == "COMPLETE":
                raise OSError("injected COMPLETE state failure")
            return original_write(path, value)

        with mock.patch.object(
            self.store, "_atomic_write_json", side_effect=fail_complete_state
        ):
            with self.assertRaisesRegex(OSError, "COMPLETE state failure"):
                self.store.complete_subject("S0001")
            with self.assertRaises(SubjectCompletedError):
                self.store.save_anthropometry("S0001", core_anthropometry())
        self.assertEqual(self.raw_state()["status"], "ACTIVE")
        self.assertEqual(self.raw_state()["anthropometry"]["revision_count"], 1)

        self.reopen_store()
        recovered = self.store.get_subject_state("S0001")
        self.assertEqual(recovered["status"], "COMPLETE")
        self.assertEqual(recovered["anthropometry"]["revision_count"], 1)
        with self.assertRaises(SubjectCompletedError):
            self.store.save_anthropometry("S0001", core_anthropometry())

    def test_daily_equipment_check_is_append_only_and_retrievable(self):
        equipment = {
            "stadiometer_id": "HEIGHT01",
            "scale_id": "SCALE01",
            "tape_id": "TAPE01",
            "anthropometer_id": "ANTHRO01",
            "equipment_check_confirmed": True,
        }
        first = self.store.save_equipment_check("OP01", equipment)
        second = self.store.save_equipment_check("OP01", equipment)
        self.assertNotEqual(first["check_id"], second["check_id"])
        self.assertTrue(first["sha256"])
        latest = self.store.get_equipment_check("OP01")
        self.assertEqual(latest["check_id"], second["check_id"])
        exact = self.store.get_equipment_check("OP01", check_id=first["check_id"])
        self.assertEqual(exact["check_id"], first["check_id"])


if __name__ == "__main__":
    unittest.main()
