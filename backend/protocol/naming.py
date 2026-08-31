"""协议规定的 ID 与文件名格式。"""

from __future__ import annotations

import re
from pathlib import PurePath

from .models import Condition


SUBJECT_ID_RE = re.compile(r"^S(?P<number>\d{4})$")
CAPTURE_STEM_RE = re.compile(
    r"^(?P<subject>S\d{4})_"
    r"(?P<camera>C336L|CD435I)_"
    r"D(?P<distance>\d{4})_"
    r"V(?P<yaw>\d{3})_"
    r"(?P<light>LSTD|LLOW|LBRI|LSL45)_"
    r"(?P<pose>P1|P2|P3)_"
    r"(?P<clothing>CF|CN)_"
    r"R(?P<repeat>\d{2})_"
    r"F(?P<frame>\d{2})$"
)

MODALITY_EXTENSIONS = {
    "rgb": ".png",
    "depth_raw": ".png",
    "depth_aligned": ".png",
    "depth_raw_npy": ".npy",
    "depth_aligned_npy": ".npy",
    "ir": ".png",
    "ir_left": ".png",
    "ir_right": ".png",
    "pointcloud_preview": ".ply",
}


def validate_subject_id(subject_id: str) -> str:
    """验证并返回形如 ``S0123`` 的匿名受试者 ID。"""

    if not isinstance(subject_id, str):
        raise TypeError("subject_id 必须是字符串")
    match = SUBJECT_ID_RE.fullmatch(subject_id)
    if match is None or int(match.group("number")) == 0:
        raise ValueError("subject_id 必须采用 S0001–S9999 格式")
    return subject_id


def format_condition_id(condition: Condition) -> str:
    """生成不含受试者和帧序号的稳定条件 ID。"""

    if not isinstance(condition, Condition):
        raise TypeError("condition 必须是 Condition")
    return (
        f"{condition.camera_code}_D{condition.distance_mm:04d}_"
        f"V{condition.view_yaw_deg:03d}_{condition.light_id}_"
        f"{condition.pose_id}_{condition.clothing_id}_R{condition.repeat_id:02d}"
    )


def format_capture_stem(
    subject_id: str, condition: Condition, frame_index: int
) -> str:
    """生成规范基础名，例如 ``S0123_C336L_..._R01_F03``。"""

    validate_subject_id(subject_id)
    if isinstance(frame_index, bool) or not isinstance(frame_index, int):
        raise TypeError("frame_index 必须是整数")
    if not 1 <= frame_index <= 5:
        raise ValueError("frame_index 必须在 1–5 范围内")
    return f"{subject_id}_{format_condition_id(condition)}_F{frame_index:02d}"


def format_modality_filename(
    subject_id: str,
    condition: Condition,
    frame_index: int,
    modality: str,
) -> str:
    """为模态目录生成文件名；模态由目录表达，不重复写入基础名。"""

    try:
        extension = MODALITY_EXTENSIONS[modality]
    except KeyError as exc:
        allowed = ", ".join(sorted(MODALITY_EXTENSIONS))
        raise ValueError(f"未知 modality: {modality!r}；允许值：{allowed}") from exc
    return f"{format_capture_stem(subject_id, condition, frame_index)}{extension}"


def parse_capture_stem(value: str) -> tuple[str, Condition, int]:
    """解析规范基础名，并返回 ``(subject_id, condition, frame_index)``。

    可以传入无扩展名的基础名，也可以传入位于模态目录中的完整文件名。
    """

    if not isinstance(value, str):
        raise TypeError("value 必须是字符串")
    stem = PurePath(value).stem
    # ``.npz``/``.png`` 只有一层扩展名；传入纯 stem 时保持不变。
    match = CAPTURE_STEM_RE.fullmatch(stem)
    if match is None:
        raise ValueError(f"不是规范采集文件名: {value!r}")

    subject_id = validate_subject_id(match.group("subject"))
    condition = Condition(
        camera_code=match.group("camera"),
        distance_mm=int(match.group("distance")),
        view_yaw_deg=int(match.group("yaw")),
        light_id=match.group("light"),
        pose_id=match.group("pose"),
        clothing_id=match.group("clothing"),
        repeat_id=int(match.group("repeat")),
        suite="parsed",
    )
    frame_index = int(match.group("frame"))
    if not 1 <= frame_index <= 5:
        raise ValueError("文件名中的 frame_index 必须在 1–5 范围内")
    return subject_id, condition, frame_index
