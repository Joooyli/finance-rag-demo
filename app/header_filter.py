"""混合解析后的唯一页眉清洗入口：原始证据保留，正文只消费 selected 块。"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import logging
import math
import re
import unicodedata

from app import config
from app.document_schema import stable_hash

logger = logging.getLogger(__name__)
FILTER_VERSION = "unified-header-v1"
_TEXT_TYPES = {"paragraph", "list_item", "heading", "caption", "footnote", "header"}
_PROTECTED_TYPES = {"table", "formula"}


def _normalized(text: str) -> str:
    # 只统一排版差异，不泛化数字；不同金额、日期、页码不能被误归成同一广告。
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"^\s*#{1,6}\s+", "", text)
    return re.sub(r"\s+", "", text).casefold()


def _valid_bbox(bbox) -> bool:
    return (isinstance(bbox, (list, tuple)) and len(bbox) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in bbox)
            and bbox[2] > bbox[0] and bbox[3] > bbox[1])


def _top(bbox, geometry: dict, ratio: float) -> bool:
    height = float(geometry.get("height") or 0)
    return bool(height > 0 and _valid_bbox(bbox) and bbox[1] >= -1 and bbox[3] <= height * ratio)


def _starts_in_top(bbox, geometry: dict, ratio: float) -> bool:
    height = float(geometry.get("height") or 0)
    return bool(height > 0 and _valid_bbox(bbox) and -1 <= bbox[1] <= height * ratio)


def _header_contains_body(block: dict, rows: list[dict] | None, geometry: dict, ratio: float) -> bool:
    """header标签不是整块删除的充分条件：大块/跨出页眉的多行可能混入正文。"""
    bbox = block.get("bbox")
    if not _valid_bbox(bbox):
        return True
    if _top(bbox, geometry, ratio):
        return False
    height = float(geometry.get("height") or 0)
    if height <= 0:
        return True
    if rows and len(rows) > 1 and any(not _top(row["bbox"], geometry, ratio) for row in rows):
        return True
    multiline = len([line for line in block.get("content_text", "").splitlines() if line.strip()]) > 1
    return bbox[3] - bbox[1] > height * ratio or (_starts_in_top(bbox, geometry, ratio) and multiline)


def _position(bbox, geometry: dict) -> tuple[float, ...] | None:
    width, height = float(geometry.get("width") or 0), float(geometry.get("height") or 0)
    if not width or not height or not _valid_bbox(bbox):
        return None
    return tuple(value / (width if index % 2 == 0 else height) for index, value in enumerate(bbox))


def _lines(block: dict) -> list[dict] | None:
    """只有逐行文本和原块精确对应、行bbox可靠时才允许拆分，绝不均分估算坐标。"""
    source = block.get("content_text", "")
    rows = block.get("lines")
    if not isinstance(rows, list) or not rows:
        return None
    result, cursor = [], 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("text"), str) or not row["text"].strip():
            return None
        text, bbox = row["text"], row.get("bbox")
        if not _valid_bbox(bbox):
            return None
        bounds = row.get("char_range")
        if bounds is not None:
            if not isinstance(bounds, (list, tuple)) or len(bounds) != 2 or any(type(v) is not int for v in bounds):
                return None
            start, end = bounds
        else:
            start = source.find(text, cursor)
            end = start + len(text)
        if start < cursor or end > len(source) or source[start:end] != text or source[cursor:start].strip():
            return None
        parent = block.get("bbox")
        if _valid_bbox(parent) and (bbox[0] < parent[0] - 2 or bbox[1] < parent[1] - 2
                                   or bbox[2] > parent[2] + 2 or bbox[3] > parent[3] + 2):
            return None
        result.append({"text": text, "bbox": list(bbox), "char_range": [start, end], "line_index": index})
        cursor = end
    return result if not source[cursor:].strip() else None


def _emit(sink, event: str, page: int, **fields):
    payload = {"stage": "block_cleaning", "engine": "header_filter", "event": event,
               "page": page, "filter_version": FILTER_VERSION, **fields}
    logger.info("[header_filter] %s page=%s block=%s reason=%s excluded=%s", event, page,
                fields.get("block_id", ""), fields.get("reason", ""), fields.get("excluded", ""),
                extra={"event_data": payload})
    if sink is not None:
        sink(payload)


def _mark_review(page: dict, block: dict, reason: str):
    block["quality_status"] = "needs_review"
    if reason not in block.setdefault("issues", []):
        block["issues"].append(reason)
    issue = {"code": reason, "block_id": block["block_id"],
             "message": "疑似广告与正文混合，缺少可精确分离的行文本或坐标，请核对原页"}
    if issue not in page.setdefault("unresolved_issues", []):
        page["unresolved_issues"].append(issue)


def _excluded(block: dict, rule: str, evidence: dict):
    block["selected"] = False
    block["excluded_reason"] = "advertising_header" if rule != "classified_footer" else "footer_removed"
    block["cleaning"] = {"version": FILTER_VERSION, "rule": rule, **evidence}


def _pattern_in_lines(text: str, patterns: list) -> bool:
    # 即使没有行bbox，也能发现混合块中有完整广告行；此时只告警，不按估算位置删除。
    return any(pattern.search(line) for line in text.splitlines() for pattern in patterns)


def _clean_context(block: dict, known_ads: set[str]) -> list[str]:
    """正文不做全局字符串替换；仅剔除与已确认广告整行相同的检索上下文字段。"""
    removed = []
    for key in ("title", "caption", "unit", "section_path"):
        original = block.get(key)
        if isinstance(original, str):
            lines = original.splitlines()
            keep = [line for line in lines if _normalized(line) not in known_ads]
            if len(keep) != len(lines):
                block.setdefault("provenance", {}).setdefault("context_before_cleaning", {})[key] = original
                block[key] = "\n".join(keep)
                removed.append(key)
        elif key == "section_path" and isinstance(original, list):
            keep = [value for value in original if not isinstance(value, str) or _normalized(value) not in known_ads]
            if keep != original:
                block.setdefault("provenance", {}).setdefault("context_before_cleaning", {})[key] = deepcopy(original)
                block[key] = keep
                removed.append(key)
    return removed


def clean_document_blocks(pages: list[dict], *, event_sink=None) -> list[dict]:
    """所有引擎完成后调用一次；整文统计重复页眉，再统一处理最终选中区域。"""
    top_ratio = float(getattr(config, "HEADER_FILTER_TOP_RATIO", .12))
    minimum_pages = int(getattr(config, "HEADER_REPEAT_MIN_PAGES", 3))
    minimum_ratio = float(getattr(config, "HEADER_REPEAT_MIN_RATIO", .6))
    tolerance = float(getattr(config, "HEADER_REPEAT_POSITION_TOLERANCE", .02))
    if not 0 < top_ratio <= .5 or minimum_pages < 2 or not 0 < minimum_ratio <= 1 or not 0 <= tolerance <= .1:
        raise ValueError("页眉清洗参数范围无效")
    patterns = [re.compile(pattern, re.I) for pattern in getattr(config, "HEADER_AD_PATTERNS", ())]
    remove_header, remove_footer = bool(config.HEADER_REMOVE), bool(config.FOOTER_REMOVE)
    # 已完成的页面不再执行第二遍过滤，避免生成重复子块和事件。
    pending = [page for page in pages if not page.get("cleaning", {}).get("completed")]
    if not pending:
        return pages
    candidates, units_by_block = defaultdict(list), {}
    for page in pending:
        geometry = page.get("geometry") or {}
        for block in page.get("blocks", []):
            if not block.get("selected", True):
                continue
            rows = _lines(block) if block.get("type") in _TEXT_TYPES else None
            units = rows if rows is not None else [{"text": block.get("content_text", ""), "bbox": block.get("bbox")}]
            units_by_block[id(block)] = rows
            if block.get("type") in _PROTECTED_TYPES | {"heading"} or block.get("type") not in _TEXT_TYPES | {"figure", "header"}:
                continue
            for unit in units:
                norm = _normalized(unit["text"])
                position = _position(unit["bbox"], geometry)
                if 4 <= len(norm) <= 120 and _top(unit["bbox"], geometry, top_ratio) and position:
                    candidates[norm].append((page["page"], position))
    # 同文字仍需处于相同位置，单页内出现多次只能贡献一个页面计数。
    repeated: dict[str, dict] = {}
    for text, observations in candidates.items():
        best, best_anchor = set(), None
        for _, anchor in observations:
            matched = {number for number, position in observations
                       if max(abs(a - b) for a, b in zip(anchor, position)) <= tolerance}
            if len(matched) > len(best):
                best, best_anchor = matched, anchor
        if len(best) >= minimum_pages and len(best) / max(1, len(pages)) >= minimum_ratio:
            repeated[text] = {"pages": sorted(best), "page_ratio": len(best) / len(pages), "position": list(best_anchor)}
    known_ads: set[str] = set()
    for page in pending:
        geometry = page.get("geometry") or {}
        changes, new_blocks = [], []
        for block in list(page.get("blocks", [])):
            if not block.get("selected", True):
                continue
            kind, text = block.get("type"), block.get("content_text", "")
            rows = units_by_block.get(id(block))
            header_contains_body = kind == "header" and _header_contains_body(block, rows, geometry, top_ratio)
            rule, evidence = None, {}
            if kind == "header" and remove_header and not header_contains_body:
                rule = "classified_header"
            elif kind in {"footer", "page_number"} and remove_footer:
                rule = "classified_footer"
            if rule:
                _excluded(block, rule, evidence)
                if rule == "classified_header" and _normalized(text):
                    known_ads.add(_normalized(text))
                    known_ads.update(_normalized(line) for line in text.splitlines() if line.strip())
                changes.append({"block_id": block["block_id"], "rule": rule, "bbox": block.get("bbox"), "text_preview": text[:120]})
                _emit(event_sink, "header_excluded", page["page"], block_id=block["block_id"], reason=rule)
                continue
            if not remove_header:
                continue
            units = rows if rows is not None else [{"text": text, "bbox": block.get("bbox")}]
            hits = []
            for index, unit in enumerate(units):
                if not _top(unit["bbox"], geometry, top_ratio):
                    continue
                norm = _normalized(unit["text"])
                position = _position(unit["bbox"], geometry)
                repeat = repeated.get(norm)
                if kind == "header" and rows is not None:
                    # header大块中只有可靠定位到页眉带的行可删除，带外行保留为正文。
                    hits.append((index, "classified_header_line", {}))
                elif (kind != "heading" and repeat and position
                        and max(abs(a - b) for a, b in zip(position, repeat["position"])) <= tolerance):
                    hits.append((index, "repeated_top_text", repeated[norm]))
                elif any(pattern.fullmatch(unit["text"].strip()) for pattern in patterns):
                    hits.append((index, "configured_top_ad", {}))
            if kind in _PROTECTED_TYPES:
                # 表格/公式可以从页眉延伸到正文；只检查整块是否位于顶部会漏掉混入的广告行。
                repeat_in_lines = any(_normalized(line) in repeated for line in text.splitlines())
                if hits or (_starts_in_top(block.get("bbox"), geometry, top_ratio)
                            and (_pattern_in_lines(text, patterns) or repeat_in_lines)):
                    _mark_review(page, block, "advertising_header_inside_structure")
                continue
            if not hits:
                # 大块跨过页眉区且包含广告关键词时，不能凭块bbox整块删除。
                bbox = block.get("bbox")
                repeat_in_lines = any(_normalized(line) in repeated for line in text.splitlines())
                if (header_contains_body or (_starts_in_top(bbox, geometry, top_ratio)
                        and (_pattern_in_lines(text, patterns) or repeat_in_lines))):
                    _mark_review(page, block, "advertising_header_not_separable")
                continue
            
            # 已知图形含正文/结构时不拆图；普通文本行有精确坐标才能安全生成保留子块。
            indexes = {index for index, _, _ in hits}
            if rows is not None and len(indexes) < len(rows):
                if kind not in _TEXT_TYPES:
                    _mark_review(page, block, "advertising_header_not_separable")
                    continue
                block["selected"] = False
                block["excluded_reason"] = "advertising_header_split"
                block["cleaning"] = {"version": FILTER_VERSION, "rule": "line_split", "removed_lines": sorted(indexes)}
                # 子块按连续保留行分组，来源范围指向原块content_text，原始块不覆盖。
                groups, current = [], []
                for index, row in enumerate(rows):
                    if index in indexes:
                        if current:
                            groups.append(current)
                            current = []
                    else:
                        current.append(row)
                if current:
                    groups.append(current)
                for group in groups:
                    start, end = group[0]["char_range"][0], group[-1]["char_range"][1]
                    child = deepcopy(block)
                    child.update(block_id=stable_hash([FILTER_VERSION, block["block_id"], start, end]),
                                 selected=True, content_text=text[start:end],
                                 bbox=[min(row["bbox"][0] for row in group), min(row["bbox"][1] for row in group),
                                       max(row["bbox"][2] for row in group), max(row["bbox"][3] for row in group)])
                    child.pop("excluded_reason", None)
                    child["cleaning"] = {"version": FILTER_VERSION, "rule": "retained_body_lines"}
                    child.setdefault("provenance", {}).update(source_block_id=block["block_id"],
                        source_char_range=[start, end], source_text_ref=f"block:{block['block_id']}#content_text")
                    if kind == "header":
                        # 原分类已证明覆盖了正文，保留行不能仍标header而被后续组装再次忽略。
                        child["type"] = "paragraph"
                        child["provenance"]["source_block_type"] = "header"
                    # 原始Markdown偏移仍保存在父块，不能让消费者用旧pos索引裁短后的子块。
                    if "raw_pos" in child["provenance"]:
                        child["provenance"]["source_raw_pos"] = child["provenance"].pop("raw_pos")
                    child["lines"] = [{**row, "char_range": [row["char_range"][0] - start, row["char_range"][1] - start]} for row in group]
                    new_blocks.append(child)
            elif len(indexes) == len(units):
                # 整个最终块都位于页眉带且完整命中规则，才排除整个块。
                _excluded(block, hits[0][1], hits[0][2])
            else:
                _mark_review(page, block, "advertising_header_not_separable")
                continue
            for index, rule, evidence in hits:
                unit = units[index]
                known_ads.add(_normalized(unit["text"]))
                change = {"block_id": block["block_id"], "rule": rule, "bbox": unit["bbox"],
                          "text_preview": unit["text"][:120], **evidence}
                changes.append(change)
                _emit(event_sink, "header_excluded", page["page"], block_id=block["block_id"], reason=rule, bbox=unit["bbox"])
        page["blocks"].extend(new_blocks)
        page["cleaning"] = {"version": FILTER_VERSION, "completed": True, "excluded": len(changes), "changes": changes}
        page["header_only"] = bool(changes) and not any(block.get("selected", True) for block in page["blocks"])
    # 所有页的广告规则确认后再清理上下文，避免后面的广告在前页标题/caption里被带回。
    for page in pending:
        for block in page.get("blocks", []):
            if block.get("selected", True) and known_ads:
                fields = _clean_context(block, known_ads)
                if fields:
                    page["cleaning"].setdefault("context_fields_cleaned", []).append({"block_id": block["block_id"], "fields": fields})
        _emit(event_sink, "cleaning_completed", page["page"], excluded=page["cleaning"]["excluded"],
              header_only=page["header_only"])
    return pages
