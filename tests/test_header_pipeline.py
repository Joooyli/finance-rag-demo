"""页眉清洗的应用链路回归：不加载模型，不写正式索引。"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pymupdf
import pytest

from app import config, embeddings, parse_log, pdf_loader, rag_pipeline
from app.document_schema import make_block
from app.parse_review import validate_indexable_run
from app.parse_router import _deduplicate


AD = "LIMITED OFFER"


def _pdf(tmp_path: Path, pages: int) -> Path:
    path = tmp_path / "header-fixture.pdf"
    with pymupdf.open() as document:
        for number in range(pages):
            document.new_page(width=600, height=800).insert_text((40, 150), f"Body page {number + 1}.")
        document.save(path)
    return path


def _page(number: int, blocks: list[dict], **extra) -> dict:
    text = "\n\n".join(block.get("content_text", "") for block in blocks)
    return {"page": number, "geometry": {"width": 600, "height": 800},
            "native_text": text, "raw_text": text, "image_count": 0,
            "drawing_count": 0, "blocks": blocks, "unresolved_issues": [], **extra}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    values = {
        "HEADER_REMOVE": True, "FOOTER_REMOVE": False,
        "HEADER_FILTER_TOP_RATIO": .12, "HEADER_REPEAT_MIN_PAGES": 3,
        "HEADER_REPEAT_MIN_RATIO": .6, "HEADER_REPEAT_POSITION_TOLERANCE": .02,
        "HEADER_AD_PATTERNS": (r"^LIMITED OFFER$",), "MINERU_ENABLED": True,
        "ENABLE_OCR": True, "PARSE_LOG_ENABLED": True,
        "DOCUMENT_DIR": tmp_path / "documents", "INDEX_REGISTRY_PATH": tmp_path / "registry.sqlite3",
        "PARSE_LOG_DIR": tmp_path / "runs", "CHUNK_SIZE": 700, "CHUNK_OVERLAP": 0,
    }
    for key, value in values.items():
        monkeypatch.setattr(config, key, value, raising=False)
    signature = {"signature": "header-fixture-model", "dimension": 3,
                 "max_seq_length": 512, "token_budget": 512}
    monkeypatch.setattr(embeddings, "get_embedding_signature", lambda: dict(signature))
    monkeypatch.setattr(embeddings, "get_token_budget", lambda: 512)
    monkeypatch.setattr(embeddings, "count_tokens", lambda text: len(text) + 2)
    monkeypatch.setattr(embeddings, "embed_texts", lambda _: pytest.fail("解析不能生成向量"))
    monkeypatch.setattr(parse_log, "_tesseract_version", lambda: "fixture")
    return tmp_path


def test_cleaning_runs_once_after_mineru_and_tesseract_before_page_sink(settings, monkeypatch):
    pdf = _pdf(settings, 3)
    bodies = {1: "The report quotes LIMITED OFFER in the body.", 2: "Second page body.", 3: "Third page body."}

    def inspect(page, layout_document, number, identity, root):
        # 三页分别提供native广告、等待MinerU接管、等待Tesseract识别的广告。
        if number == 2:
            return _page(number, [make_block(2, "formula", "old formula", [40, 250, 500, 350], "pymupdf")],
                         layout_error="fixture layout unavailable")
        header = make_block(number, "paragraph", AD if number == 1 else "", [40, 8, 500, 30], "pymupdf", order=0)
        body = make_block(number, "paragraph", bodies[number], [40, 150, 500, 200], "pymupdf", order=1)
        return _page(number, [header, body])

    monkeypatch.setattr(pdf_loader, "_inspect_page", inspect)

    def mineru(self, path, pages, output_dir, **kwargs):
        assert pages == [2]
        return {2: [make_block(2, "paragraph", AD, [40, 8, 500, 30], "mineru", order=0),
                    make_block(2, "paragraph", bodies[2], [40, 150, 500, 200], "mineru", order=1),
                    make_block(2, "formula", "x=y", [40, 250, 500, 350], "mineru", order=2, latex="x=y")]}

    monkeypatch.setattr(pdf_loader.MinerUAdapter, "parse_pages", mineru)
    monkeypatch.setattr(pdf_loader.pytesseract, "image_to_string", lambda *args, **kwargs: AD)
    calls = []
    clean = pdf_loader.clean_document_blocks

    def counted_clean(pages, **kwargs):
        calls.append(deepcopy(pages))
        return clean(pages, **kwargs)

    monkeypatch.setattr(pdf_loader, "clean_document_blocks", counted_clean)
    written, events = [], []
    pages = pdf_loader.extract_pdf_pages(pdf, artifact_dir=settings / "artifacts",
                                         page_sink=lambda page: written.append(deepcopy(page)), event_sink=events.append)
    assert len(calls) == 1
    assert len(calls[0]) == 3
    assert any(b.get("engine") == "tesseract" and b.get("selected", True) for b in calls[0][2]["blocks"])
    assert len(written) == 3
    for page in pages:
        assert bodies[page["page"]] in page["text"]
        assert AD not in page["text"].splitlines()
        excluded = [b for b in page["blocks"] if b.get("content_text") == AD and not b.get("selected", True)]
        assert excluded, "必须保留被排除广告的原始块证据"
    assert [p["text"] for p in written] == [p["text"] for p in pages]
    ocr_done = next(index for index, event in enumerate(events)
                    if event.get("engine") == "tesseract" and event.get("event") == "page_completed")
    first_exclusion = next(index for index, event in enumerate(events) if event.get("event") == "header_excluded")
    assert ocr_done < first_exclusion


def test_mixed_block_is_clean_before_logical_blocks_chunks_and_manifest(settings, monkeypatch):
    pdf = _pdf(settings, 2)
    body = "Market risk is measured in percent."

    def inspect(page, layout_document, number, identity, root):
        if number == 1:
            mixed = make_block(1, "paragraph", AD + "\n" + body, [40, 8, 500, 200], "pymupdf", lines=[
                {"text": AD, "bbox": [40, 8, 500, 30], "char_range": [0, len(AD)]},
                {"text": body, "bbox": [40, 150, 500, 200], "char_range": [len(AD) + 1, len(AD) + 1 + len(body)]},
            ])
            return _page(1, [mixed])
        return _page(2, [make_block(2, "paragraph", "Second page statement.", [40, 150, 500, 200], "pymupdf")])

    monkeypatch.setattr(pdf_loader, "_inspect_page", inspect)
    result = rag_pipeline.parse_document(pdf)
    root = Path(result.run_dir)
    assert result.quality_status == "pass"
    assert result.pages[0]["text"] == body
    assert result.chunks and all(AD not in chunk.text for chunk in result.chunks)
    logical = list(parse_log.read_jsonl(root / "logical_blocks.jsonl", strict=True))
    assert all(AD not in block["content_text"] for block in logical)
    blocks = list(parse_log.read_jsonl(root / "blocks.jsonl", strict=True))
    assert any(block.get("content_text") == AD + "\n" + body and not block.get("selected", True) for block in blocks)
    selected_body = next(block for block in blocks if block.get("selected", True) and block.get("content_text") == body)
    assert selected_body["block_id"] != blocks[0]["block_id"]
    meta, chunks, manifest = validate_indexable_run(result.run_id, require_review=False)
    assert meta["quality_status"] == "pass" and manifest["manifest_sha256"]
    assert selected_body["block_id"] in {span["block_id"] for chunk in chunks for span in chunk["source_spans"]}


def test_header_only_page_keeps_physical_record_without_false_content_failure(settings, monkeypatch):
    pdf = _pdf(settings, 2)

    def inspect(page, layout_document, number, identity, root):
        if number == 1:
            return _page(1, [make_block(1, "header", AD, [40, 8, 500, 30], "pymupdf")])
        return _page(2, [make_block(2, "paragraph", "Usable body.", [40, 150, 500, 200], "pymupdf")])

    monkeypatch.setattr(pdf_loader, "_inspect_page", inspect)
    pages = pdf_loader.extract_pdf_pages(pdf, artifact_dir=settings / "artifacts")
    assert len(pages) == 2 and pages[0]["page"] == 1
    assert pages[0]["text"] == ""
    assert pages[0]["header_only"] is True
    assert pages[0]["quality"]["status"] in {"blank", "pass"}
    assert not any(issue["code"] == "no_usable_content" for issue in pages[0]["quality"]["issues"])


def test_disabled_header_removal_retains_header_through_chunking(settings, monkeypatch):
    monkeypatch.setattr(config, "HEADER_REMOVE", False)
    pdf = _pdf(settings, 1)

    def inspect(page, layout_document, number, identity, root):
        return _page(1, [make_block(1, "header", AD, [40, 8, 500, 30], "pymupdf"),
                         make_block(1, "paragraph", "Usable body.", [40, 150, 500, 200], "pymupdf", order=1)])

    monkeypatch.setattr(pdf_loader, "_inspect_page", inspect)
    result = rag_pipeline.parse_document(pdf)
    assert AD in result.pages[0]["text"]
    assert any(chunk.text == AD for chunk in result.chunks)


def test_header_only_classification_does_not_hide_existing_failure(settings, monkeypatch):
    pdf = _pdf(settings, 1)

    def inspect(page, layout_document, number, identity, root):
        return _page(1, [make_block(1, "header", AD, [40, 8, 500, 30], "pymupdf")],
                     unresolved_issues=[{"code": "fixture_failure", "message": "独立识别错误仍未解决"}])

    monkeypatch.setattr(pdf_loader, "_inspect_page", inspect)
    record = pdf_loader.extract_pdf_pages(pdf, artifact_dir=settings / "artifacts")[0]
    assert record["text"] == ""
    assert record["quality"]["status"] == "needs_review"
    assert any(issue["code"] == "fixture_failure" for issue in record["quality"]["issues"])


@pytest.mark.parametrize("has_real_formula", [False, True])
def test_ad_reclassified_as_header_does_not_hide_real_formula_loss(settings, monkeypatch, has_real_formula):
    pdf = _pdf(settings, 1)

    def inspect(page, layout_document, number, identity, root):
        blocks = [make_block(1, "formula", AD, [40, 8, 500, 30], "pymupdf")]
        if has_real_formula:
            blocks.append(make_block(1, "formula", "V=x+y", [40, 250, 500, 280], "pymupdf"))
        return _page(1, blocks, layout_error="fixture whole-page replacement")

    monkeypatch.setattr(pdf_loader, "_inspect_page", inspect)
    monkeypatch.setattr(pdf_loader.MinerUAdapter, "parse_pages", lambda *args, **kwargs: {1: [
        make_block(1, "header", AD, [40, 8, 500, 30], "mineru"),
        make_block(1, "paragraph", "Preserved body.", [40, 150, 500, 200], "mineru", order=1),
    ]})
    page = pdf_loader.extract_pdf_pages(pdf, artifact_dir=settings / "artifacts")[0]
    assert page["text"] == "Preserved body."
    missing_structure = any(issue["code"] == "missing_complex_structure" for issue in page["quality"]["issues"])
    assert missing_structure is has_real_formula
    assert page["quality"]["status"] == ("needs_review" if has_real_formula else "pass")


def test_excluded_raw_evidence_does_not_hide_selected_replacement():
    # 未采用的原始证据可与替换结果完全相同，但不能让有效正文再次被去重排除。
    original = make_block(1, "paragraph", "Retained replacement.", [40, 150, 500, 200],
                          "pymupdf", selected=False, excluded_reason="whole_page_replaced")
    replacement = make_block(1, "paragraph", "Retained replacement.", [40, 150, 500, 200], "mineru")
    result = _deduplicate([original, replacement])
    assert result[0]["selected"] is False
    assert result[1]["selected"] is True
