"""把物理页区域组装成文档逻辑块；保留原块和每次续接证据。"""

from __future__ import annotations

from copy import deepcopy
import re

from app.document_schema import stable_hash
from app.text_utils import parse_html_table

_NEW_ITEM = re.compile(r"^\s*(?:[-*•]|\d+[.、)]|[（(]\d+[)）]|[一二三四五六七八九十]+[、.])")
_TERMINAL = re.compile(r"[。！？!?；;.]\s*$")
_TABLE_LABEL = re.compile(r"(?:表\s*[A-Za-z一二三四五六七八九十\d.-]+|Table\s*[A-Za-z\d.-]+)", re.I)


def _status(block: dict) -> str:
    return block.get("quality_status") or (block.get("quality") or {}).get("status", "pass")


def _source_span(block: dict) -> dict:
    return {
        "page": int(block["page"]), "block_id": block["block_id"],
        "bbox": list(block.get("bbox") or []),
        "text_ref": f"block:{block['block_id']}#content_text", "mapping_kind": "block",
    }


def _logical(block: dict, section: list[str]) -> dict:
    item = {
        "logical_block_id": stable_hash(["logical-v1", block["block_id"]]),
        "type": block["type"], "pages": [int(block["page"])],
        "source_block_ids": [block["block_id"]], "merge_ids": [],
        "content_text": block.get("content_text", ""),
        "section_path": list(section), "quality_status": _status(block),
        "issues": list(block.get("issues") or []),
        "source_spans": [_source_span(block)], "engine": block.get("engine", "unknown"),
    }
    for name in ("html", "html_ref", "latex", "mathml", "asset_refs", "title", "caption", "unit"):
        if name in block:
            item[name] = deepcopy(block[name])
    if block["type"] == "table":
        table = parse_html_table(block.get("html", ""))
        item["table"] = table
        for row in table["rows"]:
            row["source_spans"] = [_source_span(block)]
        if table["issues"]:
            item["quality_status"] = "needs_review"
            item["issues"].extend(table["issues"])
    return item


def _page_ok(page: dict) -> bool:
    quality = page.get("quality") or {}
    return page.get("status") not in {"failed", "blank", "empty"} and quality.get("status") not in {
        "failed", "blank", "needs_review"
    }


def _at_edges(previous: dict, following: dict, previous_page: dict, following_page: dict) -> bool:
    left, right = previous.get("bbox") or [], following.get("bbox") or []
    if len(left) != 4 or len(right) != 4:
        return False
    first_geometry, second_geometry = previous_page.get("geometry") or {}, following_page.get("geometry") or {}
    first_height = float(first_geometry.get("height") or previous_page.get("page_height") or 0)
    second_height = float(second_geometry.get("height") or following_page.get("page_height") or 0)
    return first_height > 0 and second_height > 0 and left[3] >= first_height * .78 and right[1] <= second_height * .22


def _join_text(left: str, right: str) -> tuple[str, str]:
    left, right = left.rstrip(), right.lstrip()
    # 只有英文词内断行连字符才恢复断词；金融负号、小数点和单位保持原样。
    if re.search(r"[A-Za-z]-$", left) and re.match(r"[a-z]", right):
        return left[:-1] + right, "english_line_hyphen_removed"
    separator = " " if left and right and left[-1].isascii() and right[0].isascii() and left[-1].isalnum() and right[0].isalnum() else ""
    return left + separator + right, "paragraph_continuation"


def _paragraph_evidence(previous: dict, following: dict, previous_page: dict, following_page: dict) -> dict:
    left, right = previous.get("content_text", "").strip(), following.get("content_text", "").strip()
    previous_bbox, next_bbox = previous.get("bbox") or [], following.get("bbox") or []
    width = float((following_page.get("geometry") or {}).get("width") or following_page.get("page_width") or 0)
    aligned = len(previous_bbox) == len(next_bbox) == 4 and width > 0 and abs(previous_bbox[0] - next_bbox[0]) <= width * .06
    first_font, next_font = previous.get("font_size"), following.get("font_size")
    font_compatible = first_font is None or next_font is None or abs(float(first_font) - float(next_font)) <= 1.5
    return {
        "at_page_edges": _at_edges(previous, following, previous_page, following_page),
        "left_aligned": aligned, "font_compatible": font_compatible,
        "unfinished_sentence": bool(left) and not bool(_TERMINAL.search(left)),
        "no_new_list_item": not bool(_NEW_ITEM.match(right)), "nonempty": bool(left and right),
    }


def _table_decision(left: dict, right: dict, previous: dict, following: dict, first_page: dict, next_page: dict) -> tuple[str | None, dict]:
    first_table, next_table = left["table"], right["table"]
    left_title = str(left.get("title") or left.get("caption") or "")
    right_title = str(right.get("title") or right.get("caption") or "")
    left_label, right_label = _TABLE_LABEL.search(left_title), _TABLE_LABEL.search(right_title)
    same_label = bool(left_label and right_label and re.sub(r"\s", "", left_label[0]).lower() == re.sub(r"\s", "", right_label[0]).lower())
    continuation = bool(following.get("continues_prev") or (following.get("provenance") or {}).get("continues_prev"))
    at_edges = _at_edges(previous, following, first_page, next_page)
    headers_match = bool(first_table["header_rows"] and next_table["header_rows"] and first_table["headers"] == next_table["headers"])
    columns_match = first_table["column_count"] > 0 and first_table["column_count"] == next_table["column_count"]
    units_match = not (left.get("unit") and right.get("unit")) or left.get("unit") == right.get("unit")
    evidence = {
        "continues_prev": continuation, "same_table_label": same_label, "at_page_edges": at_edges,
        "headers_match": headers_match, "columns_match": columns_match, "units_match": units_match,
        "structure_valid": not first_table["issues"] and not next_table["issues"],
    }
    if not (continuation or same_label or (at_edges and headers_match)):
        return None, evidence
    # 续表提示还需页边位置、同列结构及表头对应，不单凭相同表头拼接两个独立表。
    accepted = all((at_edges, columns_match, units_match, headers_match, evidence["structure_valid"])) and (continuation or same_label)
    return ("accepted" if accepted else "needs_review"), evidence


def assemble_document(pages: list[dict]) -> tuple[list[dict], list[dict]]:
    """只在完整物理页序列中的相邻页续接，疑似关系留待人工核对。"""
    logical_blocks: list[dict] = []
    merges: list[dict] = []
    page_map = {int(page["page"]): page for page in pages}
    by_source: dict[str, dict] = {}
    page_blocks: dict[int, list[dict]] = {}
    section: list[str] = []
    for page in sorted(pages, key=lambda item: int(item["page"])):
        # 清洗器是唯一页眉删除点：这里只消费selected，关闭开关时不得再按类型偷偷删除。
        selected = sorted((block for block in page.get("blocks", []) if block.get("selected", True)), key=lambda block: block.get("order", 0))
        page_blocks[int(page["page"])] = selected
        for block in selected:
            if block["type"] == "heading":
                level = max(1, min(6, int(block.get("level") or 1)))
                section = section[:level - 1] + [block.get("content_text", "")]
            item = _logical(block, section)
            logical_blocks.append(item)
            by_source[block["block_id"]] = item

    for page_number in sorted(page_blocks):
        if page_number + 1 not in page_blocks or not page_blocks[page_number] or not page_blocks[page_number + 1]:
            continue
        first_page, next_page = page_map[page_number], page_map[page_number + 1]
        if not _page_ok(first_page) or not _page_ok(next_page):
            continue
        previous, following = page_blocks[page_number][-1], page_blocks[page_number + 1][0]
        left, right = by_source[previous["block_id"]], by_source[following["block_id"]]
        if left is right or previous["type"] != following["type"] or left["section_path"] != right["section_path"]:
            continue
        decision: str | None = None
        evidence: dict = {}
        rule = ""
        if previous["type"] in {"paragraph", "list_item"}:
            evidence = _paragraph_evidence(previous, following, first_page, next_page)
            if all(evidence.values()):
                decision, rule = "accepted", "adjacent-paragraph-v1"
            elif all(evidence[name] for name in ("at_page_edges", "unfinished_sentence", "no_new_list_item", "nonempty")):
                decision, rule = "needs_review", "adjacent-paragraph-v1"
        elif previous["type"] == "table":
            decision, evidence = _table_decision(left, right, previous, following, first_page, next_page)
            rule = "adjacent-table-v1"
        if decision is None:
            continue
        merge_id = stable_hash([rule, previous["block_id"], following["block_id"]])
        record = {
            "merge_id": merge_id, "source_block_ids": [previous["block_id"], following["block_id"]],
            "rule": rule, "evidence": evidence, "decision": decision,
        }
        merges.append(record)
        if decision == "needs_review":
            for item in (left, right):
                item["quality_status"] = "needs_review"
                item["issues"].append("uncertain_table_continuation" if previous["type"] == "table" else "uncertain_paragraph_continuation")
                item["merge_ids"].append(merge_id)
            continue
        if previous["type"] == "table":
            skip = right["table"]["header_rows"]
            record["repeated_header_rows_removed"] = skip
            left.setdefault("table_fragments", [left.get("html", "")]).append(right.get("html", ""))
            left["table"]["rows"].extend(deepcopy(right["table"]["rows"][skip:]))
            left["html"] = "\n".join(left["table_fragments"])
            left["table"]["html"] = left["html"]
            # 保留各片段原HTML；逻辑表结构按行组合，不伪造跨页rowspan。
            left["content_text"] = left["content_text"].rstrip() + "\n" + right["content_text"].lstrip()
        else:
            left["content_text"], record["text_transform"] = _join_text(left["content_text"], right["content_text"])
        left["pages"] = sorted(set(left["pages"] + right["pages"]))
        left["source_block_ids"].extend(right["source_block_ids"])
        left["source_spans"].extend(right["source_spans"])
        left["merge_ids"].extend(right["merge_ids"] + [merge_id])
        left["logical_block_id"] = stable_hash(["logical-v1", left["source_block_ids"]])
        if left["engine"] != right["engine"]:
            left["engine"] = "hybrid"
        for source_id in right["source_block_ids"]:
            by_source[source_id] = left
        logical_blocks.remove(right)
    return logical_blocks, merges
