"""RealAnthro-RGBD-v1 协议的纯 Python 数据模型。

本模块刻意不依赖相机 SDK、Web 框架或第三方校验库，便于在采集服务、
命令行校验器和离线数据审计程序之间复用。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from itertools import combinations
from typing import Any, Mapping, Sequence


CAMERA_CODES = frozenset({"C336L", "CD435I"})
LIGHT_IDS = frozenset({"LSTD", "LLOW", "LBRI", "LSL45"})
POSE_IDS = frozenset({"P1", "P2", "P3"})
CLOTHING_IDS = frozenset({"CF", "CN"})
MEASUREMENT_KINDS = frozenset(
    {"height", "weight", "breadth", "circumference", "length"}
)


def reduce_measurement_readings(
    values: Sequence[float],
    threshold: float | None,
) -> dict[str, Any]:
    """Reduce two or three repeat readings without discarding the raw values.

    Two readings are averaged when they are within the configured project
    threshold.  A third reading is required otherwise.  Three readings use the
    closest pair; when two pairs tie, all three values are averaged so that the
    result does not depend on entry order.  A still-wide three-reading result is
    retained with ``REVIEW_REQUIRED`` rather than blocking field collection.
    """

    numeric = tuple(float(value) for value in values)
    if len(numeric) not in {2, 3}:
        raise ValueError("必须提供两次或三次测量值")
    if any(not math.isfinite(value) or value <= 0 for value in numeric):
        raise ValueError("测量值必须是大于 0 的有限数")
    numeric_threshold = float(threshold) if threshold is not None else None
    if numeric_threshold is not None and (
        not math.isfinite(numeric_threshold) or numeric_threshold <= 0
    ):
        raise ValueError("复测阈值必须大于 0")

    first_two_difference = abs(numeric[0] - numeric[1])
    third_required = (
        numeric_threshold is not None and first_two_difference > numeric_threshold
    )
    if len(numeric) == 2:
        if third_required:
            raise ValueError("前两次差值超阈值，必须录入第三次测量")
        return {
            "final_value": sum(numeric) / 2.0,
            "selected_trial_indices": [1, 2],
            "selected_difference": first_two_difference,
            "closest_pair_difference": first_two_difference,
            "first_two_difference": first_two_difference,
            "third_measurement_required": False,
            "reduction_rule": "MEAN_FIRST_TWO",
            "qc_status": "PASS_2",
        }

    pairs = tuple(combinations(range(3), 2))
    differences = {
        pair: abs(numeric[pair[0]] - numeric[pair[1]]) for pair in pairs
    }
    closest_difference = min(differences.values())
    closest_pairs = tuple(
        pair
        for pair, difference in differences.items()
        if math.isclose(difference, closest_difference, rel_tol=0.0, abs_tol=1e-9)
    )
    if len(closest_pairs) == 1:
        selected = closest_pairs[0]
        reduction_rule = "MEAN_CLOSEST_PAIR"
    else:
        selected = (0, 1, 2)
        reduction_rule = "MEAN_ALL_THREE_TIE"
    selected_values = tuple(numeric[index] for index in selected)
    selected_difference = max(selected_values) - min(selected_values)
    within_threshold = (
        numeric_threshold is None or closest_difference <= numeric_threshold
    )
    return {
        "final_value": sum(selected_values) / len(selected_values),
        "selected_trial_indices": [index + 1 for index in selected],
        "selected_difference": selected_difference,
        "closest_pair_difference": closest_difference,
        "first_two_difference": first_two_difference,
        "third_measurement_required": third_required,
        "reduction_rule": reduction_rule,
        "qc_status": "PASS_3" if within_threshold else "REVIEW_REQUIRED",
    }


def _require_plain_int(value: object, field_name: str) -> int:
    """Reject bool and non-integer values while returning a narrowed int."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} 必须是整数")
    return value


@dataclass(frozen=True)
class Condition:
    """一个不可变的采集条件。

    ``suite`` 只描述采集流程分组，不参与条件身份或文件命名。条件身份由相机、
    距离、视角、光照、姿态、服装和独立站位次数共同确定。
    """

    camera_code: str
    distance_mm: int
    view_yaw_deg: int
    light_id: str = "LSTD"
    pose_id: str = "P1"
    clothing_id: str = "CF"
    repeat_id: int = 1
    suite: str = "unspecified"

    def __post_init__(self) -> None:
        if self.camera_code not in CAMERA_CODES:
            raise ValueError(f"不支持的 camera_code: {self.camera_code!r}")

        distance_mm = _require_plain_int(self.distance_mm, "distance_mm")
        if not 250 <= distance_mm <= 6000:
            raise ValueError("distance_mm 必须在 250–6000 mm 范围内")

        yaw = _require_plain_int(self.view_yaw_deg, "view_yaw_deg")
        if not 0 <= yaw <= 359:
            raise ValueError("view_yaw_deg 必须在 0–359 范围内")

        if self.light_id not in LIGHT_IDS:
            raise ValueError(f"不支持的 light_id: {self.light_id!r}")
        if self.pose_id not in POSE_IDS:
            raise ValueError(f"不支持的 pose_id: {self.pose_id!r}")
        if self.clothing_id not in CLOTHING_IDS:
            raise ValueError(f"不支持的 clothing_id: {self.clothing_id!r}")

        repeat_id = _require_plain_int(self.repeat_id, "repeat_id")
        if not 1 <= repeat_id <= 99:
            raise ValueError("repeat_id 必须在 1–99 范围内")

        if not isinstance(self.suite, str) or not self.suite.strip():
            raise ValueError("suite 不能为空")

    @property
    def key(self) -> tuple[str, int, int, str, str, str, int]:
        """返回不含流程分组的稳定条件身份。"""

        return (
            self.camera_code,
            self.distance_mm,
            self.view_yaw_deg,
            self.light_id,
            self.pose_id,
            self.clothing_id,
            self.repeat_id,
        )

    def to_dict(self) -> dict[str, Any]:
        """返回可直接 JSON 序列化的字典。"""

        return asdict(self)


@dataclass(frozen=True)
class MeasurementDefinition:
    """一个人体测量项目及其现场复测规则。"""

    measurement_id: str
    field_names: tuple[str, ...]
    display_name_zh: str
    kind: str
    unit: str
    required: bool
    third_measurement_threshold: float | None
    required_equipment: tuple[str, ...]
    protocol_note: str = ""
    minimum_repeats: int = 2

    def __post_init__(self) -> None:
        if (
            len(self.measurement_id) != 3
            or not self.measurement_id.startswith("M")
            or not self.measurement_id[1:].isdigit()
        ):
            raise ValueError("measurement_id 必须采用 M01–M99 格式")
        if not self.field_names or any(not name for name in self.field_names):
            raise ValueError("field_names 至少包含一个非空字段名")
        if len(set(self.field_names)) != len(self.field_names):
            raise ValueError("field_names 不能重复")
        if self.kind not in MEASUREMENT_KINDS:
            raise ValueError(f"不支持的测量类别: {self.kind!r}")
        if self.unit not in {"cm", "kg"}:
            raise ValueError("unit 只能是 cm 或 kg")
        if (
            self.third_measurement_threshold is not None
            and self.third_measurement_threshold <= 0
        ):
            raise ValueError("第三次测量触发阈值必须大于 0")
        if self.minimum_repeats != 2:
            raise ValueError("V1 协议要求每个已填写项目至少测量两次")
        if not self.required_equipment:
            raise ValueError("required_equipment 不能为空")

    def needs_third_measurement(self, first: float, second: float) -> bool:
        """两次读数差值超过阈值时返回 True。"""

        if self.third_measurement_threshold is None:
            return False
        return abs(float(first) - float(second)) > self.third_measurement_threshold

    def final_value(self, values: Sequence[float]) -> float:
        """按当前复测规则计算最终 GT。

        两次合格读数取均值；若两次差值超阈值，则必须提供第三次，并选择差值
        最小的一对取均值。所有原始读数仍必须另外持久化；三次仍分散时由
        调用方同时保留 ``REVIEW_REQUIRED`` 状态。
        """

        return float(
            reduce_measurement_readings(
                values,
                self.third_measurement_threshold,
            )["final_value"]
        )

    def missing_fields(self, values: Mapping[str, object]) -> tuple[str, ...]:
        """返回此项目尚未填写的数据库字段名。"""

        return tuple(
            name
            for name in self.field_names
            if name not in values or values[name] is None or values[name] == ""
        )
