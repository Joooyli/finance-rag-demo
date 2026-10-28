"""PyMuPDF预检、区域质量路由及混合解析；所有页码均为原PDF物理页码。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import pymupdf as fitz
from PIL import Image
import pytesseract

from app import config
from app.header_filter import clean_document_blocks
from app.mineru_adapter import MinerUAdapter
from app.page_geometry import visual_bbox, visual_geometry
from app.parse_quality import COMPLEX_TYPES, area, assess_result, has_math_structure, intersection
from app.parse_router import fuse_results, ocr_block_ids, plan_routes
from app.text_utils import normalize_markdown, sanitize_text

logger = logging.getLogger(__name__)

_TYPE_MAP = {"text": "paragraph", "list-item": "list_item", "section-header": "heading", "title": "heading",
             "page-header": "header", "page-footer": "footer", "picture": "figure", "formula": "formula",
             "table": "table", "footnote": "footnote", "caption": "caption"}


def clean_text(text: str) -> str:
    """兼容旧入口，只规范空白；结构化表格/公式不经过破坏性字符清洗。"""
    return normalize_markdown(re.sub(r"[ \t]+", " ", text.replace("\x00", " ")))


def _block(page: int, kind: str, text: str, bbox: list[float], engine: str, identity: str, **extra: Any) -> dict:
    value = {"identity": identity, "page": page, "type": kind, "bbox": [round(float(v), 3) for v in bbox],
             "text": text, "engine": engine, "order": extra.get("order", 0),
             # 原生候选与layout候选即便内容几何相同也不是同一条证据。
             "representation": "layout" if "layout_class" in extra.get("provenance", {}) else "native"}
    record = {"block_id": hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True).encode()).hexdigest(),
              "page": page, "type": kind, "order": 0, "bbox": list(value["bbox"]), "engine": engine,
              "content_text": text, "quality_status": "pass", "selected": True, "provenance": {}}
    record.update(extra)
    return record


def _emit(event_sink, event: str, page: int, *, engine: str, level: str = "INFO", **fields: Any) -> None:
    payload = {"event": event, "page": page, "engine": engine, "level": level, **fields}
    getattr(logger, level.lower())("[%s] %s page=%s region=%s reason=%s chars=%s duration_ms=%s%s", engine,
        event, page, fields.get("block_id", "page"), fields.get("reason", ""), fields.get("chars", ""),
        fields.get("duration_ms", ""), f" error={fields['error']}" if fields.get("error") else "", extra={"event_data": payload})
    if event_sink is not None:
        # 事件是审阅证据，写入失败交给上层处理，不能继续假装解析成功。
        event_sink(payload)


def _check_cancel(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("解析任务已取消")


def _native_blocks(page, page_number: int, identity: str) -> tuple[list[dict], dict]:
    flags = fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES
    raw = page.get_text("dict", flags=flags, sort=True)
    blocks = []
    """结构举例{
    "width": 300.0,
    "height": 350.0,
    "blocks": [{
        "type": 0,  # 0 表示文本块
        "bbox": (50.0, 88.175, 166.171, 103.289),
        "lines": [{
            "wmode": 0,  # 0 表示水平书写
            "dir": (1.0, 0.0),  # 书写方向
            "bbox": (50.0, 88.175, 166.171, 103.289),
            "spans": [{
                "size": 11.0,
                "flags": 0,
                "font": "Helvetica",
                "color": 0,
                "origin": (50.0, 100.0),
                "text": "Some text on first page.",
                "bbox": (50.0, 88.175, 166.171, 103.289)
            }]
        }]
    }]
}"""
    for index, native in enumerate(raw.get("blocks", [])):
        if native.get("type") != 0: # 0-文本；1-图片
            continue
        lines = native.get("lines", [])
        # 行几何只用于后续统一清洗时精确拆分广告/正文；这里采集证据，不提前删页眉。
        visible_lines = []
        offset = 0
        for line in lines:
            line_text = clean_text("".join(span.get("text", "") for span in line.get("spans", [])))
            if not line_text:
                continue
            if visible_lines:
                offset += 1  # 对应下方join插入的换行，char_range始终指向content_text。
            visible_lines.append({"text": line_text, "bbox": visual_bbox(page, line["bbox"]),
                                  "char_range": [offset, offset + len(line_text)]})
            offset += len(line_text)
        text = "\n".join(line["text"] for line in visible_lines)
        spans = [span for line in lines for span in line.get("spans", [])]
        if not text:
            continue
        kind = "formula" if has_math_structure(text) else "paragraph"
        blocks.append(_block(page_number, kind, text, visual_bbox(page, native["bbox"]), "pymupdf", identity,
            order=index, font_size=max((float(s.get("size", 0)) for s in spans), default=0),
            lines=visible_lines,
            provenance={"native_bbox": list(native["bbox"]), "native_spans": spans}))
    return blocks, raw


def _layout_blocks(page, layout_doc, number: int, identity: str, native: list[dict]) -> tuple[list[dict], dict]:
    # PyMuPDF2内部会remove_rotation，因此使用独立Document；返回bbox已经是视觉方向。
    chunks = layout_doc.to_markdown(pages=[number - 1], page_chunks=True, use_ocr=False,
                                    header=True, footer=True, write_images=False, embed_images=False,
                                    show_progress=False)
    if not isinstance(chunks, list) or len(chunks) != 1:
        raise ValueError("PyMuPDF layout未返回单页结构")
    chunk = dict(chunks[0])
    raw_text = sanitize_text(str(chunk.get("text", "")))
    blocks = []
    for index, item in enumerate(chunk.get("page_boxes") or []):
        bbox = [float(v) for v in item["bbox"]]
        if area(bbox) <= 0:
            continue
        start, stop = item.get("pos", [0, 0])
        text = clean_text(raw_text[max(0, int(start)):max(0, int(stop))])
        kind = _TYPE_MAP.get(str(item.get("class", "text")), "paragraph")
        overlapping = [b for b in native if intersection(bbox, b["bbox"]) / max(1, area(b["bbox"])) > 0.3]
        # layout漏掉明显数学结构时保留原生符号线索，不只依赖栅格图片检测。
        if kind == "paragraph" and (has_math_structure(text) or any(b["type"] == "formula" for b in overlapping)):
            kind = "formula"
        record = _block(number, kind, text, bbox, "pymupdf", identity, order=index,
                        font_size=max((b.get("font_size", 0) for b in overlapping), default=0),
                        provenance={"layout_class": item.get("class"), "raw_pos": [int(start), int(stop)],
                                    "raw_text_ref": f"raw/pymupdf/page-{number:04d}.json#markdown",
                                    "coordinate_space": "visual_points"})
        # 保留header分类和原文，禁止在单一引擎适配时清理；统一过滤在融合/OCR完成后执行。
        _attach_native_lines(record, native)
        blocks.append(record)
    chunk["text"] = raw_text
    return blocks, chunk


def _attach_native_lines(block: dict, native_blocks: list[dict]) -> None:
    """将可精确对应的原生行坐标关联到最终文字，不能用新偏移覆盖引擎raw_pos。"""
    if block.get("lines") or not block.get("content_text"):
        return
    # 原生与layout证据可能指向同一行；按文字+几何去重，不把两个证据当成歧义匹配。
    evidence = {(line["text"], tuple(line["bbox"])): line
                for native in native_blocks for line in native.get("lines", [])
                if intersection(line["bbox"], block["bbox"]) / max(1, area(line["bbox"])) > 0.5}
    candidates = list(evidence.values())
    if not candidates:
        return
    text = block["content_text"]
    used = set()
    matched = []
    for match in re.finditer(r"[^\n]+", text):
        rendered = match.group()
        normalized = clean_text(rendered)
        # Markdown标题前缀只影响显示；匹配后仍记录包括前缀的真实文本范围。
        plain = re.sub(r"^#{1,6}\s+", "", normalized)
        hits = [i for i, line in enumerate(candidates) if i not in used and line["text"] == plain]
        if len(hits) != 1:
            continue  # 重复短数字或OCR改写无法唯一映射时，交清洗器保守标存疑。
        index = hits[0]
        used.add(index)
        matched.append({"text": rendered, "bbox": list(candidates[index]["bbox"]),
                        "char_range": [match.start(), match.end()]})
    if matched:
        block["lines"] = matched

def _save_asset(page, bbox: list[float], root: Path, number: int, block_id: str) -> str:
    relative = Path("assets") / f"page-{number:04d}" / f"{block_id[:16]}.png"
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    # get_pixmap的显示列表已采用页面旋转，clip使用视觉坐标；不能再次反旋转。
    clip = fitz.Rect(bbox)
    page.get_pixmap(dpi=150, clip=clip, alpha=False).save(target)
    return relative.as_posix()

## 路由检测起始
"""
_inspect_page()      页面预检
→ plan_routes()     生成路由计划
→ parse_pages()     执行MinerU并等待
→ fuse_results()    融合结果
→ ocr_block_ids()
→ _ocr_regions()    按需执行Tesseract
→ clean_document_blocks()  全部页面混合解析完成后统一清洗一次
→ assess_result()   检查最终质量
"""
def _inspect_page(page, layout_doc, number: int, identity: str, root: Path) -> dict:
    native, raw_native = _native_blocks(page, number, identity)
    native_text = "\n\n".join(b["content_text"] for b in native)
    images = page.get_image_info()
    drawings = page.get_drawings()
    record = {"schema_version": 3, "page": number, "geometry": visual_geometry(page), "native_text": native_text,
              "native_chars": len(native_text), "image_count": len(images), "drawing_count": len(drawings),
              "image_area": sum(area(visual_bbox(page, item["bbox"])) for item in images),
              "blocks": native, "raw_text": native_text, "unresolved_issues": [], "warnings": []}
    raw_layout = None
    if config.LAYOUT_ENABLED:
        try:
            layout, raw_layout = _layout_blocks(page, layout_doc, number, identity, native)
            if layout:
                for block in native:
                    block["selected"] = False
                    block["excluded_reason"] = "layout_reading_order"
                record["blocks"] = native + layout
                record["raw_text"] = raw_layout["text"]
        except Exception as exc:
            record["layout_error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("[pymupdf] layout_failed page=%s error=%s", number, exc)
    # 缺少layout候选时图片必须成为可路由区域；整页未知扫描不能直接降为普通OCR。
    if not raw_layout or not raw_layout.get("page_boxes"):
        for index, image in enumerate(images):
            bbox = visual_bbox(page, image["bbox"])
            record["blocks"].append(_block(number, "figure", "", bbox, "pymupdf", identity,
                                             order=len(record["blocks"]), provenance={"image_index": index}))
    selected = [b for b in record["blocks"] if b.get("selected", True)]
    # 矢量表格没有图片xref，用线框提议补充layout候选；不把table_strategy传入不支持的layout接口。
    if len(drawings) >= 4 and not any(b["type"] == "table" for b in selected):
        try:
            for table in page.find_tables(strategy=config.TABLE_STRATEGY).tables:
                bbox = visual_bbox(page, table.bbox)
                record["blocks"].append(_block(number, "table", "", bbox, "pymupdf", identity,
                    order=len(record["blocks"]), provenance={"detector": "find_tables", "strategy": config.TABLE_STRATEGY}))
        except Exception as exc:
            record["warnings"].append({"code": "table_detector_failed", "message": str(exc)})
    for block in record["blocks"]:
        if block.get("selected", True) and block["type"] in COMPLEX_TYPES:
            try:
                block["asset_refs"] = [_save_asset(page, block["bbox"], root, number, block["block_id"])]
            except Exception as exc:
                record["warnings"].append({"code": "region_preview_failed", "message": str(exc), "block_id": block["block_id"]})
    raw_path = root / "raw" / "pymupdf" / f"page-{number:04d}.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    # ensure_ascii保留所有原始字符的可写转义，不让代理字符导致半条审阅产物。
    raw_path.write_text(json.dumps({"native": raw_native, "layout": raw_layout, "markdown": record["raw_text"]},
                                  ensure_ascii=True, default=str), encoding="utf-8")
    record["raw_ref"] = raw_path.relative_to(root).as_posix()
    record["header_blocks"] = sum(b["type"] == "header" for b in record["blocks"])
    record["footer_blocks"] = sum(b["type"] in {"footer", "page_number"} for b in record["blocks"])
    return record


def _ocr_regions(page, record: dict, identity: str, event_sink, cancel_event, block_ids=None) -> None:
    requested = set(ocr_block_ids(record["blocks"]) if block_ids is None else block_ids)
    candidates = [b for b in record["blocks"] if b.get("selected", True) and b["block_id"] in requested
                  and b["type"] in {"paragraph", "list_item", "heading", "footnote"}]
    if not candidates:
        return
    if not config.ENABLE_OCR:
        record["unresolved_issues"].append({"code": "ocr_disabled", "message": "普通文字需要OCR，但ENABLE_OCR=false"})
        return
    if config.TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD
    for block in candidates:
        _check_cancel(cancel_event)
        started = time.monotonic()
        info = {"block_id": block["block_id"], "reason": "ordinary_text_fallback", "stage": "ocr"}
        _emit(event_sink, "page_started", record["page"], engine="tesseract", **info)
        try:
            clip = fitz.Rect(block["bbox"])
            pix = page.get_pixmap(dpi=config.OCR_DPI, clip=clip, alpha=False)
            image = Image.open(BytesIO(pix.tobytes("png"))).convert("RGB")
            text = clean_text(pytesseract.image_to_string(image, lang=config.OCR_LANG, config="--psm 6",
                                                         timeout=getattr(config, "OCR_TIMEOUT_SECONDS", 120)))
            if not text:
                raise ValueError("Tesseract没有返回可用文字")
            block["selected"] = False
            block["excluded_reason"] = "ordinary_region_ocr_replaced"
            replacement = _block(record["page"], block["type"], text, block["bbox"], "tesseract", identity,
                order=block["order"], font_size=block.get("font_size", 0),
                provenance={"source_block_id": block["block_id"], "dpi": config.OCR_DPI, "psm": 6, "language": config.OCR_LANG})
            record["blocks"].append(replacement)
            _emit(event_sink, "page_completed", record["page"], engine="tesseract", chars=len(text),
                  duration_ms=round((time.monotonic() - started) * 1000), **info)
        except Exception as exc:
            _emit(event_sink, "page_failed", record["page"], engine="tesseract", level="ERROR", error=str(exc),
                  duration_ms=round((time.monotonic() - started) * 1000), **info)
            block["quality_status"] = "needs_review"
            record["unresolved_issues"].append({"code": "tesseract_failed", "block_id": block["block_id"], "message": str(exc)})


def _prefix_mineru_refs(blocks: list[dict]) -> None:
    for block in blocks:
        if block.get("provenance", {}).get("raw_ref"):
            block["provenance"]["raw_ref"] = (Path("raw/mineru") / block["provenance"]["raw_ref"]).as_posix()
        if block.get("html_ref"):
            block["html_ref"] = (Path("raw/mineru") / block["html_ref"]).as_posix()
        if block.get("asset_refs"):
            block["asset_refs"] = [(Path("raw/mineru") / value).as_posix() for value in block["asset_refs"]]


def _expected_content_types(record: dict, original_blocks: list[dict]) -> set[str]:
    """仅解除能和已清理广告逐区域对应的结构期待，正文表格/公式丢失仍须报错。"""
    removed = [block for block in record["blocks"]
               if block.get("excluded_reason") == "advertising_header"]

    def is_confirmed_ad(original: dict) -> bool:
        # 引擎可能把同一广告先判formula、后判header；类型不同不能造成虚假的公式缺失。
        # 必须同时有非空同文和近乎相同区域，不因“页面删过页眉”放过其他真实公式。
        text = re.sub(r"[\s$#]+", "", original.get("content_text", "")).casefold()
        if not text:
            return False
        for excluded in removed:
            if text != re.sub(r"[\s$#]+", "", excluded.get("content_text", "")).casefold():
                continue
            overlap = intersection(original["bbox"], excluded["bbox"])
            union = area(original["bbox"]) + area(excluded["bbox"]) - overlap
            if union > 0 and overlap / union >= 0.8:
                return True
        return False

    return {block["type"] for block in original_blocks if not is_confirmed_ad(block)}


def extract_pdf_pages(pdf_path: Path, page_sink=None, *, artifact_dir: Path | None = None, event_sink=None,
                      run_id: str | None = None, doc_id: str | None = None, cancel_event=None) -> list[dict]:
    """返回完整页面序列；等待MinerU完成后融合，审阅产物写失败不会被吞掉。"""
    pdf_path = Path(pdf_path)
    root = Path(artifact_dir) if artifact_dir is not None else Path(tempfile.mkdtemp(prefix="finance-parse-"))
    root.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    records: list[dict] = []
    # layout修改内存页面旋转，必须和原件几何/渲染使用的Document分开。
    with fitz.open(pdf_path) as doc, fitz.open(pdf_path) as layout_doc:
        for number in range(1, doc.page_count + 1):
            _check_cancel(cancel_event)
            page = doc[number - 1]
            try:
                # 页面预检
                record = _inspect_page(page, layout_doc, number, identity, root)
            except Exception as exc:
                record = {"page": number, "geometry": visual_geometry(page), "blocks": [], "native_text": "",
                          "inspection_error": str(exc), "unresolved_issues": [{"code": "inspection_failed", "message": str(exc)}]}
                _emit(event_sink, "page_failed", number, engine="pymupdf", level="ERROR", error=str(exc), stage="inspection")
            record.update({"schema_version": 3, "source": pdf_path.name, "run_id": run_id, "doc_id": doc_id})
            records.append(record)
        # 路由
        routes = plan_routes(records)
        mineru_pages = []
        for record in records:
            number = record["page"]
            record["route_plan"] = routes[number]
            record["quality"] = routes[number]["quality"]
            record["route"] = routes[number]["route"]
            _emit(event_sink, "route_selected", number, engine="router", stage="routing", route=record["route"],
                  reason=",".join(routes[number]["reason_codes"]))
            if record["route"].startswith("mineru"):
                mineru_pages.append(number)
        mineru_results: dict[int, list[dict]] = {}
        mineru_error: Exception | None = None
        if mineru_pages:
            try:
                if not getattr(config, "MINERU_ENABLED", True):
                    raise RuntimeError("复杂区域需要MinerU，但MINERU_ENABLED=false")
                mineru_results = MinerUAdapter().parse_pages(pdf_path, mineru_pages, root / "raw" / "mineru",
                                                            event_sink=event_sink, cancel_event=cancel_event)
            except InterruptedError:
                raise
            except Exception as exc:
                mineru_error = exc
                logger.exception("[mineru] batch_failed pages=%s error=%s", mineru_pages, exc)
        expected_by_page = {}
        for record in records:
            _check_cancel(cancel_event)
            expected_by_page[record["page"]] = [b for b in record["blocks"] if b.get("selected", True)]
            if record["page"] in mineru_pages:
                converted = mineru_results.get(record["page"], [])
                if converted:
                    _prefix_mineru_refs(converted)
                    record["blocks"], record["route"] = fuse_results(record, converted)
                else:
                    message = str(mineru_error) if mineru_error else "MinerU未返回该页有效结构"
                    record["unresolved_issues"].append({"code": "mineru_failed", "message": message})
                    # 保留原始区域供回看，但禁止将失败复杂区域伪装成合格正文。
                    for block in record["blocks"]:
                        if block.get("selected", True) and block["type"] in COMPLEX_TYPES:
                            block["quality_status"] = "needs_review"
            # MinerU融合后再检查最终普通区域；复杂页也可同时保留native并局部Tesseract兜底。
            # 整页MinerU接管后重新计算ID，不会误用已被排除的原生候选。
            fallback_ids = ocr_block_ids(record["blocks"])
            if fallback_ids:
                _ocr_regions(doc[record["page"] - 1], record, identity, event_sink, cancel_event, fallback_ids)
            # 为MinerU/OCR最终块关联能精确匹配的原生行；扫描页无证据时不编造行坐标。
            native_evidence = [b for b in record["blocks"] if b.get("engine") == "pymupdf" and b.get("lines")]
            for block in record["blocks"]:
                if block.get("selected", True):
                    _attach_native_lines(block, native_evidence)

        # 唯一内容清洗入口：所有页的融合/OCR已经完成，此时可联合跨页重复证据。
        # 必须先清洗，再构造页面text、评估最终质量和调用page_sink；跨页/切块只看到干净正文。
        clean_document_blocks(records, event_sink=event_sink)
        for record in records:
            _check_cancel(cancel_event)
            expected = _expected_content_types(record, expected_by_page[record["page"]])
            selected = sorted((b for b in record["blocks"] if b.get("selected", True)), key=lambda b: (b.get("order", 0), b["bbox"][1], b["bbox"][0]))
            if record["route"] == "hybrid":
                # 两个引擎的order不是同一个序列；可靠单栏区域融合后按视觉位置统一排序。
                selected.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))
                for order, block in enumerate(selected):
                    block.setdefault("provenance", {})["engine_order"] = block.get("order", 0)
                    block["order"] = order
            record["text"] = "\n\n".join(b.get("content_text", "") for b in selected if b.get("content_text", "").strip())
            record["chars"] = len(record["text"])
            record["raw_chars"] = len(record.get("raw_text", ""))
            record["engines_used"] = sorted({b.get("engine", "pymupdf") for b in selected})
            if len(record["engines_used"]) > 1:
                record["route"] = "hybrid"
            record["ocr_used"] = "tesseract" in record["engines_used"]
            record["ocr_engines"] = ["tesseract"] if record["ocr_used"] else []
            record["mineru_used"] = "mineru" in record["engines_used"]
            record["mode"] = "mixed" if len(record["engines_used"]) > 1 else (record["engines_used"][0] if record["engines_used"] else "empty")
            record["quality"] = assess_result(record, expected)
            record["status"] = "failed" if record.get("inspection_error") else ("blank" if record["quality"]["status"] == "blank" else "completed")
            record["block_ids"] = [block["block_id"] for block in selected]
            record["elements"] = [{"index": b.get("order", 0), "class": b["type"], "bbox": b["bbox"], "text": b.get("content_text", "")[:config.PARSE_LOG_ELEMENT_TEXT_MAX], "selected": b.get("selected", True)} for b in record["blocks"]] if config.PARSE_LOG_INCLUDE_ELEMENTS else []
            if page_sink is not None:
                page_sink(record)
            _emit(event_sink, "page_finalized", record["page"], engine="router", stage="fusion", route=record["route"],
                  chars=record["chars"], quality_status=record["quality"]["status"])
    return records
