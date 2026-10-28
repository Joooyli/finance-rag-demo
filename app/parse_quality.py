"""混合解析的可解释质量检查；规则分数不代表金融事实正确概率。"""

from __future__ import annotations

import re
from typing import Any

from app import config


COMPLEX_TYPES = {"table", "formula", "figure"}
DECORATIVE_TYPES = {"header", "footer", "page_number"}


def area(bbox: list[float] | tuple[float, ...]) -> float:
    return max(0.0, float(bbox[2]) - float(bbox[0])) * max(0.0, float(bbox[3]) - float(bbox[1]))


def intersection(a: list[float], b: list[float]) -> float:
    return area([max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])])


def invalid_char_ratio(text: str) -> float:
    invalid = sum(ch == "\ufffd" or 0xD800 <= ord(ch) <= 0xDFFF or (ord(ch) < 32 and ch not in "\n\r\t") for ch in text)
    return invalid / max(1, len(text))


def assess_page(page: dict[str, Any]) -> dict[str, Any]:
    """预检基于几何、文字与结构联合判断，少字标题页不会单独触发OCR。"""
    geometry = page.get("geometry") or {}
    width, height = float(geometry.get("width", 0)), float(geometry.get("height", 0))
    page_area = max(1.0, width * height)
    # 只评估被选中的块；未提供 selected 时默认参与检查。
    blocks = [b for b in page.get("blocks", []) if b.get("selected", True)]
    native = page.get("native_text", "")
    issues: list[dict] = []
    # bbox 为 [左, 上, 右, 下]；要求四个坐标、正面积，且不超出页面边界超过 1 point。
    for block in blocks:
        bbox = block.get("bbox") or []
        if len(bbox) != 4 or not area(bbox) or bbox[0] < -1 or bbox[1] < -1 or bbox[2] > width + 1 or bbox[3] > height + 1:
            issues.append({"code": "invalid_geometry", "block_id": block.get("block_id"), "message": "区域坐标无法映射回可见页面"})
    # 统计替代字符、代理码点和异常控制字符；超过配置阈值时标记待复核。
    invalid = invalid_char_ratio(native)
    if invalid > getattr(config, "MAX_INVALID_CHAR_RATIO", 0.01):
        issues.append({"code": "invalid_characters", "message": "原生文字包含过多替代字符或控制字符"})
    # 复杂块包括表格、公式和图像；面积直接相加，重叠区域不会去重，占比上限为 1。
    complex_area = sum(area(b["bbox"]) for b in blocks if b.get("type") in COMPLEX_TYPES)
    metrics = {
        "native_chars": len(native),
        "invalid_char_ratio": round(invalid, 6),
        "complex_area_ratio": round(min(1.0, complex_area / page_area), 6),
        "image_area_ratio": round(min(1.0, float(page.get("image_area", 0)) / page_area), 6),
        # 统计有文字内容的块的边界框面积，而非文字笔画实际覆盖面积。
        "text_area_ratio": round(min(1.0, sum(area(b["bbox"]) for b in blocks if b.get("content_text")) / page_area), 6),
        "drawing_count": int(page.get("drawing_count", 0)),
    }
    # 只有文字、图片、绘图和选中块全部缺失时才判为空白页，避免误判扫描页。
    blank = not native.strip() and not page.get("image_count") and not page.get("drawing_count") and not blocks
    # pass 仅表示未触发本方法的检查规则，不保证识别内容或金融事实正确。
    return {"status": "blank" if blank else ("needs_review" if issues else "pass"), "issues": issues, "metrics": metrics,
            "rule_version": getattr(config, "ROUTING_RULE_VERSION", "hybrid-v1")}


def assess_result(page: dict[str, Any], expected_types: set[str] | None = None) -> dict[str, Any]:
    """最终结构检查不掩盖引擎失败；复杂结构缺失必须交人工处理。"""
    quality = assess_page({**page, "native_text": page.get("text", "")})
    issues = list(page.get("unresolved_issues", [])) + quality["issues"]
    selected = [b for b in page.get("blocks", []) if b.get("selected", True) and b.get("type") not in DECORATIVE_TYPES]
    # 纯广告/装饰页清空后仍保留物理页记录，不因原页有图片或绘图而误报漏识别。
    # unresolved_issues仍在下方合并，不能用header_only掩盖OCR/MinerU等实际失败。
    if page.get("header_only") and not any(b.get("selected", True) for b in page.get("blocks", [])):
        quality["status"] = "blank"
    types = {b.get("type") for b in selected}
    for kind in (expected_types or set()) & {"table", "formula"}:
        if kind not in types:
            issues.append({"code": "missing_complex_structure", "message": f"预检识别到{kind}，最终结果未恢复对应结构"})
    for block in selected:
        if block.get("quality_status") in {"needs_review", "failed"}:
            issues.append({"code": "block_needs_review", "block_id": block.get("block_id"), "message": "区域识别仍需核对"})
        text = block.get("content_text", "")
        if block.get("type") == "table" and not (block.get("html") or block.get("html_ref") or "|" in text):
            issues.append({"code": "table_structure_missing", "block_id": block.get("block_id"), "message": "表格缺少可核对行列结构"})
        if block.get("type") == "formula" and not (block.get("latex") or block.get("mathml") or text.strip()):
            issues.append({"code": "formula_content_missing", "block_id": block.get("block_id"), "message": "公式未恢复表达式"})
        if block.get("type") == "figure" and not block.get("asset_refs"):
            issues.append({"code": "figure_asset_missing", "block_id": block.get("block_id"), "message": "图像缺少可回看的区域资产"})
    if not page.get("text", "").strip() and quality["status"] != "blank" and not any(b.get("type") == "figure" and b.get("asset_refs") for b in selected):
        issues.append({"code": "no_usable_content", "message": "可见内容未得到文字或可追溯图像"})
    # 同一错误可能来自页面及块检查；去重保留具体来源，避免日志被重复问题淹没。
    unique = {str((item.get("code"), item.get("block_id"), item.get("message"))): item for item in issues}
    quality["issues"] = list(unique.values())
    quality["status"] = "needs_review" if unique else quality["status"]
    return quality


def has_math_structure(text: str) -> bool:
    """检测明显结构符号；普通数字、百分号或单个比较号不视为复杂公式。"""
    return bool(re.search(r"[∑∏∫√∂]|\\(?:frac|sum|sqrt|int)\b", text))
