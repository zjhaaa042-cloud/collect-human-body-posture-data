"""RealAnthro-RGBD-v1 人体测量字段字典。"""

from __future__ import annotations

from .models import MeasurementDefinition


def _measurement_definitions() -> tuple[MeasurementDefinition, ...]:
    tape = ("non_stretch_tape",)
    return (
        MeasurementDefinition(
            "M01", ("height_cm",), "身高", "height", "cm", True, 1.0,
            ("stadiometer",),
            "赤足、脚跟并拢、身体直立，头保持 Frankfurt plane；测量地面到头顶点 Vertex。",
        ),
        MeasurementDefinition(
            "M02", ("weight_kg",), "体重", "weight", "kg", False, None,
            ("calibrated_scale",),
            "穿标准轻薄采集服；原规范未给第三测阈值，两次差异只记录和警告。",
        ),
        MeasurementDefinition(
            "M03", ("biacromial_breadth_cm",), "肩峰间宽", "breadth", "cm",
            True, 1.0, ("anthropometer_or_large_sliding_caliper",),
            "定位左、右 Acromion 肩峰点，测量两点间直线距离；禁止用软尺沿皮肤绕量。",
        ),
        MeasurementDefinition(
            "M04", ("shoulder_girth_cm",), "肩围", "circumference", "cm", False, 1.0,
            tape, "软尺环绕左右三角肌最突出位置。",
        ),
        MeasurementDefinition(
            "M05", ("upper_chest_circumference_cm",), "上胸围", "circumference", "cm",
            False, 1.0, tape, "靠近腋下的水平面。",
        ),
        MeasurementDefinition(
            "M06", ("nipple_chest_circumference_cm",), "胸围",
            "circumference", "cm", True, 2.0, tape,
            "取左右 thelion（乳头点）的平均高度，在该水平面绕身体一圈；放松站立、不说话，软尺水平、贴身不勒，每次均在正常呼气末读数。",
        ),
        MeasurementDefinition(
            "M07", ("underbust_circumference_cm",), "下胸围", "circumference", "cm",
            False, 1.0, tape, "胸部下缘水平。",
        ),
        MeasurementDefinition(
            "M08", ("natural_waist_circumference_cm",), "自然腰围", "circumference", "cm",
            False, 1.0, tape, "人体自然最窄腰部。",
        ),
        MeasurementDefinition(
            "M09", ("midpoint_waist_circumference_cm",), "腰围",
            "circumference", "cm", True, 1.5, tape,
            "左右两侧分别取最低可触肋骨与髂嵴最高点之间的中点，以左右中点确定水平面并绕量一圈；放松站立、不说话，每次均在正常呼气末读数。",
        ),
        MeasurementDefinition(
            "M10", ("umbilical_circumference_cm",), "脐围", "circumference", "cm",
            False, 1.0, tape, "经过肚脐水平。",
        ),
        MeasurementDefinition(
            "M11", ("max_abdomen_circumference_cm",), "最大腹围", "circumference", "cm",
            False, 1.0, tape, "腹部最大水平围度。",
        ),
        MeasurementDefinition(
            "M12", ("max_hip_circumference_cm",), "臀围", "circumference", "cm",
            True, 1.5, tape,
            "放松站立、不说话，软尺水平并上下移动，寻找臀部最大水平周长，每次均在正常呼气末读数。",
        ),
        MeasurementDefinition(
            "M13", ("trochanter_pelvis_circumference_cm",), "大转子/骨盆围",
            "circumference", "cm", False, 1.0, tape, "经过左右大转子附近。",
        ),
        MeasurementDefinition(
            "M14", ("high_hip_circumference_cm",), "上臀围", "circumference", "cm",
            False, 1.0, tape, "启用前必须冻结可复现的 landmark 定义。",
        ),
        MeasurementDefinition(
            "M15", ("neck_circumference_cm",), "颈围", "circumference", "cm", False,
            1.0, tape,
        ),
        MeasurementDefinition(
            "M16",
            ("left_upper_arm_circumference_cm", "right_upper_arm_circumference_cm"),
            "左右上臂围", "circumference", "cm", False, 1.0, tape,
        ),
        MeasurementDefinition(
            "M17",
            ("left_forearm_circumference_cm", "right_forearm_circumference_cm"),
            "左右前臂围", "circumference", "cm", False, 1.0, tape,
        ),
        MeasurementDefinition(
            "M18",
            ("left_max_thigh_circumference_cm", "right_max_thigh_circumference_cm"),
            "左右最大大腿围", "circumference", "cm", False, 1.0, tape,
        ),
        MeasurementDefinition(
            "M19",
            ("left_max_calf_circumference_cm", "right_max_calf_circumference_cm"),
            "左右最大小腿围", "circumference", "cm", False, 1.0, tape,
        ),
        MeasurementDefinition(
            "M20", ("arm_span_cm",), "臂展", "length", "cm", False, 0.5,
            ("anthropometer_or_wall_scale",),
        ),
        MeasurementDefinition(
            "M21", ("left_arm_length_cm", "right_arm_length_cm"), "左右臂长", "length",
            "cm", False, 0.5, ("non_stretch_tape",),
        ),
        MeasurementDefinition(
            "M22", ("inseam_cm",), "内侧腿长", "length", "cm", False, 0.5,
            ("anthropometer_or_non_stretch_tape",),
            "仅在操作员能够标准化测量时启用。",
        ),
        MeasurementDefinition(
            "M23", ("acromion_to_midpoint_waist_cm",), "肩峰至中点腰躯干长", "length",
            "cm", False, 0.5, ("non_stretch_tape",),
            "启用时必须固定测量侧或明确记录左右侧。",
        ),
    )


MEASUREMENT_DEFINITIONS = _measurement_definitions()
MEASUREMENTS_BY_ID = {
    definition.measurement_id: definition for definition in MEASUREMENT_DEFINITIONS
}

if len(MEASUREMENTS_BY_ID) != len(MEASUREMENT_DEFINITIONS):
    raise RuntimeError("人体测量字典中存在重复 measurement_id")


def measurement_definitions() -> tuple[MeasurementDefinition, ...]:
    """返回按 M01–M23 排序的不可变字段字典。"""

    return MEASUREMENT_DEFINITIONS


def required_measurements() -> tuple[MeasurementDefinition, ...]:
    """返回必须完整填写的五项人工测量。"""

    return tuple(item for item in MEASUREMENT_DEFINITIONS if item.required)


def optional_measurements() -> tuple[MeasurementDefinition, ...]:
    """返回允许整项留空的其余人工测量。"""

    return tuple(item for item in MEASUREMENT_DEFINITIONS if not item.required)
