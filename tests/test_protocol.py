"""backend.protocol 的纯标准库单元测试。"""

from __future__ import annotations

import unittest
from collections import Counter

from backend.protocol import (
    Condition,
    MEASUREMENTS_BY_ID,
    format_capture_stem,
    format_condition_id,
    format_modality_filename,
    full31_no_lux,
    full36,
    gemini27,
    measurement_definitions,
    optional_measurements,
    parse_capture_stem,
    primary3,
    reduce_measurement_readings,
    required_measurements,
    validate_conditions,
    validate_subject_id,
)


class ConditionMatrixTests(unittest.TestCase):
    def test_profile_counts_and_uniqueness(self) -> None:
        profiles = {
            "primary3": (primary3(), 3),
            "gemini27": (gemini27(), 27),
            "full31_no_lux": (full31_no_lux(), 31),
            "full36": (full36(), 36),
        }
        for name, (conditions, expected_count) in profiles.items():
            with self.subTest(profile=name):
                self.assertEqual(len(conditions), expected_count)
                self.assertEqual(len({item.key for item in conditions}), expected_count)

    def test_primary3_are_independent_repositioning_conditions(self) -> None:
        conditions = primary3()
        self.assertEqual([item.repeat_id for item in conditions], [1, 2, 3])
        self.assertTrue(
            all(
                (
                    item.camera_code,
                    item.distance_mm,
                    item.view_yaw_deg,
                    item.light_id,
                    item.pose_id,
                    item.clothing_id,
                )
                == ("C336L", 2500, 0, "LSTD", "P1", "CF")
                for item in conditions
            )
        )

    def test_gemini27_breakdown(self) -> None:
        suite_counts = Counter(item.suite for item in gemini27())
        self.assertEqual(
            suite_counts,
            {
                "gemini_view": 12,
                "gemini_distance": 4,
                "gemini_repositioning": 2,
                "gemini_lighting": 3,
                "gemini_pose": 2,
                "gemini_natural_clothing": 4,
            },
        )

    def test_gemini_workflow_starts_in_natural_clothing_then_changes_once(self) -> None:
        conditions = gemini27()
        self.assertTrue(all(item.clothing_id == "CN" for item in conditions[:4]))
        self.assertTrue(all(item.clothing_id == "CF" for item in conditions[4:]))

    def test_full36_camera_counts(self) -> None:
        camera_counts = Counter(item.camera_code for item in full36())
        self.assertEqual(camera_counts, {"C336L": 27, "CD435I": 9})

    def test_no_lux_profile_removes_exactly_five_nonstandard_lights(self) -> None:
        complete = {item.key: item for item in full36()}
        no_lux = {item.key: item for item in full31_no_lux()}
        self.assertLess(set(no_lux), set(complete))

        removed = [complete[key] for key in set(complete) - set(no_lux)]
        self.assertEqual(len(removed), 5)
        self.assertEqual(
            Counter(item.camera_code for item in removed),
            {"C336L": 3, "CD435I": 2},
        )
        self.assertTrue(all(item.light_id != "LSTD" for item in removed))
        self.assertTrue(all(item.light_id == "LSTD" for item in no_lux.values()))

    def test_duplicate_validation_ignores_suite_label(self) -> None:
        first = Condition("C336L", 2500, 0, suite="first")
        duplicate = Condition("C336L", 2500, 0, suite="second")
        with self.assertRaisesRegex(ValueError, "重复"):
            validate_conditions((first, duplicate))

    def test_condition_rejects_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            Condition("UNKNOWN", 2500, 0)
        with self.assertRaises(TypeError):
            Condition("C336L", True, 0)
        with self.assertRaises(ValueError):
            Condition("C336L", 2500, 360)
        with self.assertRaises(ValueError):
            Condition("C336L", 2500, 0, light_id="DARK")


class NamingTests(unittest.TestCase):
    def test_normative_gemini_example(self) -> None:
        condition = Condition("C336L", 2500, 0)
        self.assertEqual(
            format_capture_stem("S0123", condition, 3),
            "S0123_C336L_D2500_V000_LSTD_P1_CF_R01_F03",
        )

    def test_normative_d435i_example(self) -> None:
        condition = Condition("CD435I", 3000, 90)
        self.assertEqual(
            format_capture_stem("S0123", condition, 3),
            "S0123_CD435I_D3000_V090_LSTD_P1_CF_R01_F03",
        )

    def test_condition_id_excludes_subject_and_frame(self) -> None:
        condition = Condition("C336L", 2500, 0, repeat_id=2)
        self.assertEqual(
            format_condition_id(condition),
            "C336L_D2500_V000_LSTD_P1_CF_R02",
        )

    def test_modality_filename_and_parse_round_trip(self) -> None:
        condition = Condition(
            "C336L",
            4000,
            330,
            light_id="LSL45",
            pose_id="P3",
            clothing_id="CN",
            repeat_id=2,
            suite="test",
        )
        filename = format_modality_filename(
            "S9999", condition, 5, modality="depth_raw"
        )
        self.assertTrue(filename.endswith(".png"))
        subject_id, parsed, frame_index = parse_capture_stem(filename)
        self.assertEqual(subject_id, "S9999")
        self.assertEqual(parsed.key, condition.key)
        self.assertEqual(frame_index, 5)

    def test_invalid_subject_frame_and_modality_are_rejected(self) -> None:
        condition = Condition("C336L", 2500, 0)
        for invalid in ("S0000", "S123", "s0123", "张三"):
            with self.subTest(subject_id=invalid), self.assertRaises(ValueError):
                validate_subject_id(invalid)
        with self.assertRaises(ValueError):
            format_capture_stem("S0001", condition, 0)
        with self.assertRaises(ValueError):
            format_capture_stem("S0001", condition, 6)
        with self.assertRaises(ValueError):
            format_modality_filename("S0001", condition, 3, "unknown")


class MeasurementDefinitionTests(unittest.TestCase):
    def test_required_and_optional_ranges(self) -> None:
        self.assertEqual(
            [item.measurement_id for item in required_measurements()],
            ["M01", "M03", "M06", "M09", "M12"],
        )
        self.assertEqual(
            [item.measurement_id for item in optional_measurements()],
            [
                f"M{index:02d}"
                for index in range(1, 24)
                if index not in {1, 3, 6, 9, 12}
            ],
        )
        self.assertEqual(len(measurement_definitions()), 23)

    def test_measurement_ids_and_fields_are_unique(self) -> None:
        definitions = measurement_definitions()
        self.assertEqual(len({item.measurement_id for item in definitions}), 23)
        all_fields = [field for item in definitions for field in item.field_names]
        self.assertEqual(len(all_fields), len(set(all_fields)))

    def test_required_measurement_thresholds_match_field_policy(self) -> None:
        self.assertEqual(
            {
                item.measurement_id: item.third_measurement_threshold
                for item in required_measurements()
            },
            {"M01": 1.0, "M03": 1.0, "M06": 2.0, "M09": 1.5, "M12": 1.5},
        )

    def test_circumference_requires_third_reading_over_threshold(self) -> None:
        waist = MEASUREMENTS_BY_ID["M08"]
        self.assertFalse(waist.needs_third_measurement(80.0, 81.0))
        self.assertTrue(waist.needs_third_measurement(80.0, 81.01))
        self.assertAlmostEqual(waist.final_value((80.0, 80.8)), 80.4)
        with self.assertRaisesRegex(ValueError, "第三次"):
            waist.final_value((80.0, 82.0))

    def test_three_readings_use_closest_pair(self) -> None:
        waist = MEASUREMENTS_BY_ID["M08"]
        self.assertAlmostEqual(waist.final_value((80.0, 83.0, 80.4)), 80.2)

    def test_three_reading_reduction_preserves_qc_status(self) -> None:
        resolved = reduce_measurement_readings((84.0, 88.0, 85.0), 2.0)
        self.assertAlmostEqual(resolved["final_value"], 84.5)
        self.assertEqual(resolved["selected_trial_indices"], [1, 3])
        self.assertEqual(resolved["qc_status"], "PASS_3")

        dispersed = reduce_measurement_readings((80.0, 84.0, 88.0), 2.0)
        self.assertAlmostEqual(dispersed["final_value"], 84.0)
        self.assertEqual(dispersed["selected_trial_indices"], [1, 2, 3])
        self.assertAlmostEqual(dispersed["selected_difference"], 8.0)
        self.assertAlmostEqual(dispersed["closest_pair_difference"], 4.0)
        self.assertEqual(dispersed["qc_status"], "REVIEW_REQUIRED")
        self.assertAlmostEqual(
            reduce_measurement_readings((88.0, 84.0, 80.0), 2.0)["final_value"],
            84.0,
        )

    def test_weight_has_no_invented_third_measurement_threshold(self) -> None:
        weight = MEASUREMENTS_BY_ID["M02"]
        self.assertIsNone(weight.third_measurement_threshold)
        self.assertFalse(weight.needs_third_measurement(60.0, 62.0))
        self.assertAlmostEqual(weight.final_value((60.0, 62.0)), 61.0)
        self.assertAlmostEqual(weight.final_value((60.0, 62.0, 60.2)), 60.1)

    def test_bilateral_optional_item_reports_missing_fields(self) -> None:
        upper_arm = MEASUREMENTS_BY_ID["M16"]
        missing = upper_arm.missing_fields(
            {"left_upper_arm_circumference_cm": 29.5}
        )
        self.assertEqual(missing, ("right_upper_arm_circumference_cm",))


if __name__ == "__main__":
    unittest.main()
