"""Strict RGB/raw-depth/aligned-depth contract for formal captures."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Mapping

import numpy as np


class FrameContractError(ValueError):
    """Raised when a frame cannot prove its color or alignment semantics."""


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value):
        return asdict(value)
    return {}


def _intrinsic_values(value: Any) -> dict[str, float | int]:
    item = _mapping(value)
    try:
        return {
            "fx": float(item["fx"]),
            "fy": float(item["fy"]),
            "cx": float(item["cx"]),
            "cy": float(item["cy"]),
            "width": int(item["width"]),
            "height": int(item["height"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise FrameContractError("相机内参字段不完整或无效") from exc


def validate_frame_contract(
    frame: Any,
    camera_code: str,
    *,
    max_stream_timestamp_skew_ms: float = 75.0,
) -> dict[str, Any]:
    """Validate one formal RGB-D frame and return auditable evidence.

    ``depth_raw`` remains in the native depth-camera coordinates. Only
    ``depth_aligned`` is required to share RGB pixel coordinates.
    """

    errors: list[str] = []
    color = getattr(frame, "color", None)
    depth_raw = getattr(frame, "depth_raw", None)
    depth_aligned = getattr(frame, "depth_aligned", None)
    metadata = _mapping(getattr(frame, "camera_metadata", {}))

    if not isinstance(color, np.ndarray) or color.dtype != np.uint8 or color.ndim != 3 or color.shape[2] != 3:
        errors.append("RGB 必须为 uint8 HxWx3")
    if not isinstance(depth_raw, np.ndarray) or depth_raw.dtype != np.uint16 or depth_raw.ndim != 2:
        errors.append("raw depth 必须为 uint16 HxW")
    if not isinstance(depth_aligned, np.ndarray) or depth_aligned.dtype != np.uint16 or depth_aligned.ndim != 2:
        errors.append("aligned depth 必须为 uint16 HxW")
    if isinstance(color, np.ndarray) and isinstance(depth_aligned, np.ndarray):
        if color.shape[:2] != depth_aligned.shape:
            errors.append("aligned depth 尺寸必须与 RGB 完全一致")

    color_order = str(metadata.get("rgb_color_order") or "").upper()
    if color_order != "RGB":
        errors.append(f"相机输出色序必须显式声明为 RGB，当前为 {color_order or '未声明'}")

    intrinsics = _mapping(getattr(frame, "intrinsics", {}))
    intrinsic_values: dict[str, dict[str, float | int]] = {}
    for name, array in (
        ("color", color),
        ("depth_raw", depth_raw),
        ("depth_aligned", depth_aligned),
    ):
        try:
            values = _intrinsic_values(intrinsics.get(name))
            intrinsic_values[name] = values
            if isinstance(array, np.ndarray) and (
                values["height"], values["width"]
            ) != array.shape[:2]:
                errors.append(f"{name} 内参尺寸与数组尺寸不一致")
            if values["fx"] <= 0 or values["fy"] <= 0:
                errors.append(f"{name} fx/fy 必须为正数")
        except FrameContractError:
            errors.append(f"缺少或无法解析 {name} 内参")

    if "color" in intrinsic_values and "depth_aligned" in intrinsic_values:
        color_intrinsic = intrinsic_values["color"]
        aligned_intrinsic = intrinsic_values["depth_aligned"]
        for name in ("fx", "fy", "cx", "cy"):
            if not np.isclose(
                float(color_intrinsic[name]),
                float(aligned_intrinsic[name]),
                rtol=1e-7,
                atol=1e-4,
            ):
                errors.append(f"aligned depth 的 {name} 与 RGB 内参不一致")
        if (
            color_intrinsic["width"],
            color_intrinsic["height"],
        ) != (
            aligned_intrinsic["width"],
            aligned_intrinsic["height"],
        ):
            errors.append("aligned depth 与 RGB 内参分辨率不一致")

    extrinsics = _mapping(getattr(frame, "extrinsics", {}))
    raw_to_color = _mapping(extrinsics.get("depth_raw_to_color"))
    if len(raw_to_color.get("rotation") or []) != 9:
        errors.append("缺少有效的 depth_raw_to_color 旋转矩阵")
    if len(raw_to_color.get("translation") or []) != 3:
        errors.append("缺少有效的 depth_raw_to_color 平移向量")

    timestamps = _mapping(getattr(frame, "stream_timestamps", {}))
    timestamp_values: dict[str, float] = {}
    for name in ("color", "depth_raw", "depth_aligned"):
        try:
            value = float(timestamps[name])
            if not np.isfinite(value):
                raise ValueError
            timestamp_values[name] = value
        except (KeyError, TypeError, ValueError):
            errors.append(f"缺少有效的 {name} 流时间戳")
    stream_skew_ms = None
    if len(timestamp_values) == 3:
        stream_skew_ms = max(timestamp_values.values()) - min(timestamp_values.values())
        if stream_skew_ms > float(max_stream_timestamp_skew_ms):
            errors.append(
                f"RGB/raw/aligned 流时间差 {stream_skew_ms:.3f} ms 超过 "
                f"{max_stream_timestamp_skew_ms:.3f} ms"
            )

    frame_numbers = _mapping(getattr(frame, "stream_frame_numbers", {}))
    raw_number = frame_numbers.get("depth_raw")
    aligned_number = frame_numbers.get("depth_aligned")
    if raw_number is None or aligned_number is None:
        errors.append("raw/aligned depth 缺少流帧号")
    else:
        try:
            if int(raw_number) != int(aligned_number):
                errors.append("raw/aligned depth 不是由同一深度帧生成")
        except (TypeError, ValueError):
            errors.append("raw/aligned depth 流帧号无效")

    try:
        depth_scale = float(getattr(frame, "depth_scale"))
        if not np.isfinite(depth_scale) or depth_scale <= 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("depth_scale 必须为有限正数")

    if errors:
        raise FrameContractError(f"{camera_code} RGB-D 对齐契约失败：" + "；".join(errors))

    return {
        "schema_version": "rgbd-frame-contract-v1",
        "camera_code": camera_code,
        "rgb_color_order": "RGB",
        "rgb_storage_color_order": "sRGB",
        "spatial_alignment": {
            "depth_raw_coordinate_system": "native_depth_camera",
            "depth_aligned_coordinate_system": "color_camera",
            "depth_aligned_matches_rgb_pixels": True,
            "raw_to_color_extrinsics_present": True,
            "aligned_intrinsics_match_rgb": True,
        },
        "temporal_alignment": {
            "source": "single_sdk_frameset",
            "stream_timestamp_skew_ms": stream_skew_ms,
            "maximum_allowed_ms": float(max_stream_timestamp_skew_ms),
            "raw_aligned_depth_frame_number_equal": True,
        },
    }


__all__ = ["FrameContractError", "validate_frame_contract"]
