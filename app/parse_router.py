"""页面/区域路由与唯一正文所有权，避免原生文字和整页OCR双份入库。"""

from __future__ import annotations

from copy import deepcopy
from difflib import SequenceMatcher
import re
from typing import Any

from app import config
from app.parse_quality import COMPLEX_TYPES, DECORATIVE_TYPES, area, assess_page, intersection, invalid_char_ratio

ORDINARY_TYPES = {"paragraph", "list_item", "heading", "footnote"}


def ocr_block_ids(blocks: list[dict]) -> list[str]:
    """只修复明确普通文字区域，复杂块内部的候选文字不交普通OCR覆盖。"""
    selected = [block for block in blocks if block.get("selected", True)]
    complex_blocks = [block for block in selected if block.get("type") in COMPLEX_TYPES]
    result = []
    for block in selected:
        if block.get("type") not in ORDINARY_TYPES:
            continue
        text = block.get("content_text", "")
        if text.strip() and invalid_char_ratio(text) <= getattr(config, "MAX_INVALID_CHAR_RATIO", 0.01):
            continue
        if any(intersection(block["bbox"], owner["bbox"]) / max(1.0, area(block["bbox"])) >= 0.3 for owner in complex_blocks):
            continue
        result.append(block["block_id"])
    return result


def _columns(blocks: list[dict], width: float) -> bool:
    text = [b for b in blocks if b.get("content_text") and b.get("type") == "paragraph"]
    for i, a in enumerate(text):
        for b in text[i + 1:]:
            left, right = sorted((a["bbox"], b["bbox"]), key=lambda box: box[0])
            vertical_overlap = max(0, min(left[3], right[3]) - max(left[1], right[1]))
            if right[0] - left[2] > width * 0.06 and vertical_overlap > min(left[3] - left[1], right[3] - right[1]) * 0.4:
                return True
    return False


def plan_page(page: dict[str, Any]) -> dict[str, Any]:
    quality = assess_page(page)
    selected = [b for b in page.get("blocks", []) if b.get("selected", True) and b.get("type") not in DECORATIVE_TYPES]
    complex_blocks = [b for b in selected if b.get("type") in COMPLEX_TYPES]
    ordinary_fallback = ocr_block_ids(selected)
    width = float(page.get("geometry", {}).get("width", 0))
    multi_column = _columns(selected, width)
    reasons: list[str] = []
    if page.get("inspection_error"):
        return {"route": "failed", "reason_codes": ["inspection_failed"], "quality": quality, "complex_block_ids": [], "ocr_block_ids": []}
    if quality["status"] == "blank":
        return {"route": "blank", "reason_codes": ["blank_page"], "quality": quality, "complex_block_ids": [], "ocr_block_ids": []}
    if page.get("layout_error"):
        reasons.append("layout_unavailable")
    if multi_column:
        reasons.append("complex_reading_order")
    if page.get("drawing_count", 0) >= 25 and not complex_blocks:
        reasons.append("dense_vector_graphics")
    if complex_blocks:
        reasons.extend(sorted({f"{b['type']}_detected" for b in complex_blocks}))
    if reasons or complex_blocks:
        whole_page = bool(page.get("layout_error") or multi_column or "dense_vector_graphics" in reasons or quality["metrics"]["complex_area_ratio"] > getattr(config, "ROUTING_COMPLEX_AREA_RATIO", 0.35) or not page.get("native_text", "").strip())
        return {"route": "mineru_page" if whole_page else "mineru_region", "reason_codes": reasons,
                "quality": quality, "complex_block_ids": [b["block_id"] for b in complex_blocks], "ocr_block_ids": ordinary_fallback}
    if ordinary_fallback or quality["status"] == "needs_review" or not page.get("native_text", "").strip():
        # 只有layout明确给出纯普通文字区域时才走Tesseract，未知扫描页面交MinerU。
        ordinary = bool(ordinary_fallback) and all(b.get("type") in ORDINARY_TYPES for b in selected)
        return {"route": "tesseract" if ordinary else "mineru_page", "reason_codes": ["ordinary_text_fallback" if ordinary else "unknown_scanned_page"], "quality": quality, "complex_block_ids": [], "ocr_block_ids": ordinary_fallback}
    return {"route": "native", "reason_codes": ["native_quality_pass"], "quality": quality, "complex_block_ids": [], "ocr_block_ids": []}


def plan_routes(pages: list[dict]) -> dict[int, dict]:
    # plan_page 
    plans = {int(page["page"]): plan_page(page) for page in pages}
    page_map = {int(page["page"]): page for page in pages}
    for number, page in page_map.items():
        following = page_map.get(number + 1)
        if following is None or plans[number + 1]["route"] in {"blank", "failed"}:
            continue
        height = float(page.get("geometry", {}).get("height", 0))
        bottom_table = any(block.get("selected", True) and block.get("type") == "table" and
                           block["bbox"][3] >= height * 0.78 for block in page.get("blocks", []))
        # 仅显式续表线索扩展邻页上下文；不能因前页有表就把后续整文档送入MinerU。
        continuation = bool(re.search(r"续表|表[^\n]{0,30}[（(]?续[）)]?|continued\s+table|table[^\n]{0,30}continued",
                                      following.get("native_text", ""), re.I))
        if bottom_table and continuation and not plans[number + 1]["route"].startswith("mineru"):
            plans[number + 1].update(route="mineru_page", reason_codes=["possible_table_continuation"])
    return plans


def _deduplicate(blocks: list[dict]) -> list[dict]:
    selected: list[dict] = []
    for block in blocks:
        # 原始证据selected=false不能反过来把真正采用的替换块判成重复。
        if not block.get("selected", True):
            continue
        duplicate = False
        for prior in selected:
            denominator = max(1.0, min(area(block["bbox"]), area(prior["bbox"])))
            if intersection(block["bbox"], prior["bbox"]) / denominator < 0.9:
                continue
            if block.get("type") == prior.get("type") and block.get("content_text") and SequenceMatcher(None, block["content_text"], prior.get("content_text", "")).ratio() >= 0.95:
                duplicate = True
                break
        if duplicate:
            block["selected"] = False
            block["excluded_reason"] = "overlapping_duplicate"
        else:
            selected.append(block)
    return blocks


def fuse_results(page: dict[str, Any], mineru_blocks: list[dict]) -> tuple[list[dict], str]:
    """区域匹配不可靠则整页替换，原候选仍保留但不再作为正文。"""
    native = deepcopy(page.get("blocks", []))
    converted = deepcopy(mineru_blocks)
    for block in converted:
        block.setdefault("selected", True)
        # 此处仅融合区域所有权；所有引擎完成后统一按开关清理页眉页脚。
    owners = [b for b in native if b.get("selected", True) and b.get("type") in COMPLEX_TYPES]
    chosen: list[dict] = []
    region_safe = page.get("route_plan", {}).get("route") == "mineru_region" and bool(owners)
    for block in converted:
        if not block.get("selected", True):
            continue
        matches = [owner for owner in owners if intersection(block["bbox"], owner["bbox"]) / max(1.0, min(area(block["bbox"]), area(owner["bbox"]))) >= 0.5]
        if matches:
            chosen.append(block)
            if block.get("type") == "figure" and not block.get("asset_refs"):
                # 向量图不一定有MinerU图片文件，保留原PDF区域裁图供人工回看。
                block["asset_refs"] = list(dict.fromkeys(ref for owner in matches for ref in owner.get("asset_refs", [])))
            # 同一个MinerU块同时穿过复杂区和独立正文区时，区域所有权不再可靠。
            if any(b.get("selected", True) and b.get("type") not in COMPLEX_TYPES | DECORATIVE_TYPES and intersection(block["bbox"], b["bbox"]) / max(1.0, area(b["bbox"])) > 0.3 for b in native):
                region_safe = False
    if any(not any(intersection(owner["bbox"], block["bbox"]) / max(1.0, min(area(owner["bbox"]), area(block["bbox"]))) >= 0.5 for block in chosen) for owner in owners):
        region_safe = False
    if region_safe:
        for block in native:
            if block.get("selected", True) and any(intersection(block["bbox"], owner["bbox"]) / max(1.0, area(block["bbox"])) >= 0.3 for owner in owners):
                block["selected"] = False
                block["excluded_reason"] = "complex_region_replaced"
        for block in converted:
            if block not in chosen and block.get("selected", True):
                block["selected"] = False
                block["excluded_reason"] = "native_region_retained"
        route = "hybrid"
    else:
        for block in native:
            if block.get("selected", True):
                block["selected"] = False
                block["excluded_reason"] = "whole_page_replaced"
        route = "mineru"
    return _deduplicate(native + converted), route
