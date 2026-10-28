"""把不同解析引擎的区域位置统一到旋转后的可见页面 point 坐标。"""

from __future__ import annotations

import math
from collections.abc import Sequence

import pymupdf


def _bbox_values(bbox: Sequence[float]) -> list[float]:
    if len(bbox) != 4 or any(isinstance(v, bool) for v in bbox):
        raise ValueError("bbox must contain four finite numbers")
    values = [float(v) for v in bbox]
    if not all(math.isfinite(v) for v in values):
        raise ValueError("bbox must contain four finite numbers")
    if values[2] <= values[0] or values[3] <= values[1]:
        raise ValueError("bbox must have positive width and height")
    return values


def visual_geometry(page) -> dict:
    """get_text 的坐标已经相对 cropbox；只需应用 rotation_matrix，不能再减裁剪原点。"""
    rect = page.rect
    return {
        "width": float(rect.width),
        "height": float(rect.height),
        "rotation": int(page.rotation),
        "mediabox": list(page.mediabox),
        "cropbox": list(page.cropbox),
        "rotation_matrix": list(page.rotation_matrix),
        "derotation_matrix": list(page.derotation_matrix),
        "coordinate_space": "visual_page_points",
        "transform_version": 1,
    }


def visual_bbox(page, bbox: Sequence[float]) -> list[float]:
    """原生文字 bbox → 旋转后的视觉页面；layout 的视觉 bbox 不应再调用本函数。"""
    rect = pymupdf.Rect(_bbox_values(bbox)) * page.rotation_matrix
    rect &= page.rect
    if rect.is_empty or rect.is_infinite:
        raise ValueError("bbox lies outside the visible PDF page")
    return [round(v, 6) for v in rect]


def normalized_bbox(bbox: Sequence[float], geometry: dict) -> list[float]:
    """MinerU 4 Middle schema 2.0 bbox 为 0..1；不是 PDF point 或 0..1000。"""
    values = _bbox_values(bbox)
    if not all(0.0 <= v <= 1.0 for v in values):
        raise ValueError("MinerU bbox must use normalized coordinates in [0, 1]")
    width, height = float(geometry["width"]), float(geometry["height"])
    if not math.isfinite(width + height) or width <= 0 or height <= 0:
        raise ValueError("invalid page geometry")
    return [round(values[0] * width, 6), round(values[1] * height, 6),
            round(values[2] * width, 6), round(values[3] * height, 6)]
