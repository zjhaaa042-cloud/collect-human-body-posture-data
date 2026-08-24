"""RealAnthro-RGBD-v1 协议的纯 Python 数据模型。

本模块刻意不依赖相机 SDK、Web 框架或第三方校验库，便于在采集服务、
命令行校验器和离线数据审计程序之间复用。
"""

from __future__ import annotations

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
        """按 V1 规则计算最终 GT。

        两次合格读数取均值；若两次差值超阈值，则必须提供第三次，并选择差值
        最小的一对取均值。所有原始读数仍必须另外持久化。
        """

        numeric = tuple(float(value) for value in values)
        if len(numeric) not in {2, 3}:
            raise ValueError("必须提供两次或三次测量值")
        if any(value <= 0 for value in numeric):
            raise ValueError("测量值必须大于 0")
        if len(numeric) == 2:
            if self.needs_third_measurement(numeric[0], numeric[1]):
                raise ValueError("前两次差值超阈值，必须录入第三次测量")
            return sum(numeric) / 2

        indexed_pairs = combinations(enumerate(numeric), 2)
        (_, first), (_, second) = min(
            indexed_pairs,
            key=lambda pair: (abs(pair[0][1] - pair[1][1]), pair[0][0], pair[1][0]),
        )
        return (first + second) / 2

    def missing_fields(self, values: Mapping[str, object]) -> tuple[str, ...]:
        """返回此项目尚未填写的数据库字段名。"""

        return tuple(
            name
            for name in self.field_names
            if name not in values or values[name] is None or values[name] == ""
        )
