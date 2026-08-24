"""RealAnthro-RGBD-v1 的条件矩阵生成器。"""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from .models import Condition


def validate_conditions(
    conditions: Iterable[Condition], *, expected_count: int | None = None
) -> tuple[Condition, ...]:
    """冻结并严格验证一个条件矩阵。

    重复判断不包含 ``suite``，因此不能用不同流程标签掩盖实际重复条件。
    """

    frozen = tuple(conditions)
    if not frozen:
        raise ValueError("条件矩阵不能为空")
    if any(not isinstance(condition, Condition) for condition in frozen):
        raise TypeError("条件矩阵只能包含 Condition")

    counts = Counter(condition.key for condition in frozen)
    duplicates = tuple(key for key, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"条件矩阵存在重复项: {duplicates!r}")
    if expected_count is not None and len(frozen) != expected_count:
        raise ValueError(
            f"条件矩阵数量错误：期望 {expected_count}，实际 {len(frozen)}"
        )
    return frozen


def primary3() -> tuple[Condition, ...]:
    """Gemini 核心正面条件的 3 次独立重新站位。"""

    return validate_conditions(
        (
            Condition(
                camera_code="C336L",
                distance_mm=2500,
                view_yaw_deg=0,
                repeat_id=repeat_id,
                suite="primary_repositioning",
            )
            for repeat_id in (1, 2, 3)
        ),
        expected_count=3,
    )


def gemini27() -> tuple[Condition, ...]:
    """生成 Gemini 336L 正式全量 27 条条件。"""

    conditions: list[Condition] = []

    # 先拍受试者入场时的日常服，之后只换一次标准贴身服；避免在整套
    # Gemini 流程末尾再次换回日常服。
    conditions.extend(
        Condition(
            "C336L",
            2500,
            yaw,
            clothing_id="CN",
            suite="gemini_natural_clothing",
        )
        for yaw in (0, 90, 180, 270)
    )

    # 12-view：基线正面条件就是 R01，不再额外重复添加。
    conditions.extend(
        Condition("C336L", 2500, yaw, suite="gemini_view")
        for yaw in range(0, 360, 30)
    )

    # 2.5 m 已包含在 12-view 中。
    conditions.extend(
        Condition("C336L", distance, 0, suite="gemini_distance")
        for distance in (1500, 2000, 3000, 4000)
    )

    # R01 已包含在 12-view 的正面条件中；这里只加入真正重新站位的 R02/R03。
    conditions.extend(
        Condition(
            "C336L", 2500, 0, repeat_id=repeat_id, suite="gemini_repositioning"
        )
        for repeat_id in (2, 3)
    )

    # 标准光已包含在基线中，额外加入低光、亮光和左前 45° 侧光。
    conditions.extend(
        Condition("C336L", 2500, 0, light_id=light, suite="gemini_lighting")
        for light in ("LLOW", "LBRI", "LSL45")
    )

    conditions.extend(
        Condition("C336L", 2500, 0, pose_id=pose, suite="gemini_pose")
        for pose in ("P2", "P3")
    )

    return validate_conditions(conditions, expected_count=27)


def _d435i9() -> tuple[Condition, ...]:
    """生成 Full-36 中 D435i 的 9 条跨设备条件。"""

    conditions: list[Condition] = []
    conditions.extend(
        Condition("CD435I", 3000, yaw, suite="d435i_view")
        for yaw in (0, 90, 180, 270)
    )
    conditions.append(Condition("CD435I", 2500, 0, suite="d435i_distance"))
    conditions.extend(
        Condition(
            "CD435I", 3000, 0, repeat_id=repeat_id, suite="d435i_repositioning"
        )
        for repeat_id in (2, 3)
    )
    conditions.extend(
        Condition("CD435I", 3000, 0, light_id=light, suite="d435i_lighting")
        for light in ("LLOW", "LBRI")
    )
    return validate_conditions(conditions, expected_count=9)


def full36() -> tuple[Condition, ...]:
    """完整目标矩阵：Gemini 27 + D435i 9。需要照度计和受控灯光。"""

    return validate_conditions((*gemini27(), *_d435i9()), expected_count=36)


def full31_no_lux() -> tuple[Condition, ...]:
    """当前无照度计时的可执行矩阵。

    从 Full-36 移除全部 5 个非标准光照实验条件；仍保留作为基础环境标签的
    ``LSTD``，但不能据此宣称已完成定量光照鲁棒性实验。
    """

    conditions = (
        condition
        for condition in full36()
        if condition.suite not in {"gemini_lighting", "d435i_lighting"}
    )
    return validate_conditions(conditions, expected_count=31)


# 同时提供动词形式，便于调用方按项目编码风格选择。
generate_primary3 = primary3
generate_gemini27 = gemini27
generate_full31_no_lux = full31_no_lux
generate_full36 = full36
