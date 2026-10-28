import logging

import pymupdf
import pytest
from PIL import Image, ImageStat

from app import config
from app import pdf_loader


@pytest.fixture
def sample_pdf(tmp_path):
    path = tmp_path / "sample.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=800)
        page.insert_text((40, 60), "Short native title")
        doc.new_page(width=600, height=800)
        doc.save(path)
    return path


def inspected(page, layout_doc, number, identity, root, *, kind="paragraph", text="", image_count=0):
    blocks = [] if number == 2 else [pdf_loader._block(number, kind, text, [40, 40, 550, 700], "pymupdf", identity)]
    return {"page": number, "geometry": {"width": 600, "height": 800}, "native_text": text,
            "image_count": image_count if blocks else 0, "drawing_count": 0, "blocks": blocks,
            "unresolved_issues": []}


def test_preserves_short_native_and_blank_pages_without_ocr(sample_pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LAYOUT_ENABLED", False)
    monkeypatch.setattr(pdf_loader.pytesseract, "image_to_string", lambda *a, **kw: pytest.fail("native title must not trigger OCR"))
    records = pdf_loader.extract_pdf_pages(sample_pdf, artifact_dir=tmp_path / "run")
    assert len(records) == 2
    assert records[0]["route"] == "native"
    assert records[0]["text"] == "Short native title"
    assert records[1]["quality"]["status"] == "blank"


def test_page_sink_failure_is_not_silently_swallowed(sample_pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LAYOUT_ENABLED", False)
    def failed_sink(record):
        raise OSError("review artifact unavailable")
    with pytest.raises(OSError, match="review artifact"):
        pdf_loader.extract_pdf_pages(sample_pdf, page_sink=failed_sink, artifact_dir=tmp_path / "run")


def test_tesseract_page_logs_include_start_completion_and_physical_page(sample_pdf, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(pdf_loader, "_inspect_page", inspected)
    monkeypatch.setattr(config, "ENABLE_OCR", True)
    monkeypatch.setattr(pdf_loader.pytesseract, "image_to_string", lambda *a, **kw: "Recovered ordinary text")
    events = []
    with caplog.at_level(logging.INFO):
        records = pdf_loader.extract_pdf_pages(sample_pdf, artifact_dir=tmp_path / "run", event_sink=events.append)
    ocr_events = [event for event in events if event["engine"] == "tesseract"]
    assert [event["event"] for event in ocr_events] == ["page_started", "page_completed"]
    assert all(event["page"] == 1 for event in ocr_events)
    assert "page_started page=1" in caplog.text
    assert "page_completed page=1" in caplog.text
    assert records[0]["text"] == "Recovered ordinary text"
    assert records[0]["ocr_used"] is True
    assert records[0]["quality"]["status"] == "pass"


def test_tesseract_failure_logs_page_and_blocks_quality_pass(sample_pdf, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(pdf_loader, "_inspect_page", inspected)
    monkeypatch.setattr(config, "ENABLE_OCR", True)
    def fail_ocr(*args, **kwargs):
        raise RuntimeError("missing language")
    monkeypatch.setattr(pdf_loader.pytesseract, "image_to_string", fail_ocr)
    with caplog.at_level(logging.INFO):
        records = pdf_loader.extract_pdf_pages(sample_pdf, artifact_dir=tmp_path / "run")
    assert "page_failed page=1" in caplog.text
    assert "missing language" in caplog.text
    assert records[0]["quality"]["status"] == "needs_review"


def test_complex_failure_keeps_evidence_and_never_falls_back_to_tesseract(sample_pdf, tmp_path, monkeypatch):
    def inspect_formula(*args):
        return inspected(*args, kind="formula", text="bad formula")
    monkeypatch.setattr(pdf_loader, "_inspect_page", inspect_formula)
    monkeypatch.setattr(pdf_loader.pytesseract, "image_to_string", lambda *a, **kw: pytest.fail("formula must not use ordinary OCR"))
    def fail_mineru(*args, **kwargs):
        raise RuntimeError("missing model")
    monkeypatch.setattr(pdf_loader.MinerUAdapter, "parse_pages", fail_mineru)
    records = pdf_loader.extract_pdf_pages(sample_pdf, artifact_dir=tmp_path / "run")
    assert records[0]["quality"]["status"] == "needs_review"
    assert any(issue["code"] == "mineru_failed" for issue in records[0]["quality"]["issues"])
    assert records[0]["blocks"][0]["content_text"] == "bad formula"


def test_layout_explicitly_disables_implicit_ocr_and_preserves_raw_pos():
    calls = []
    class FakeLayout:
        def to_markdown(self, **kwargs):
            calls.append(kwargs)
            return [{"text": "Header\n\nBody", "page_boxes": [
                {"class": "page-header", "bbox": [40, 10, 150, 30], "pos": [0, 8]},
                {"class": "text", "bbox": [40, 40, 500, 70], "pos": [8, 12]}]}]
    blocks, raw = pdf_loader._layout_blocks(None, FakeLayout(), 7, "hash", [])
    assert calls[0]["pages"] == [6]
    assert calls[0]["use_ocr"] is False
    assert raw["text"] == "Header\n\nBody"
    assert blocks[1]["content_text"] == "Body"
    assert blocks[1]["provenance"]["raw_pos"] == [8, 12]


def test_mineru_replaces_whole_page_without_native_duplicate(sample_pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_loader, "_inspect_page", lambda *args: inspected(*args, kind="formula", text="Bad original"))
    def mineru(*args, **kwargs):
        return {1: [pdf_loader._block(1, "formula", "x = y", [40, 40, 550, 700], "mineru", "hash", latex="x=y")]}
    monkeypatch.setattr(pdf_loader.MinerUAdapter, "parse_pages", mineru)
    records = pdf_loader.extract_pdf_pages(sample_pdf, artifact_dir=tmp_path / "run")
    assert records[0]["text"] == "x = y"
    assert records[0]["quality"]["status"] == "pass"
    assert records[0]["mineru_used"] is True


def test_rotated_region_preview_uses_visual_clip_without_second_rotation(tmp_path):
    with pymupdf.open() as doc:
        page = doc.new_page(width=200, height=100)
        page.draw_rect(pymupdf.Rect(10, 10, 70, 30), color=(0, 0, 0), fill=(0, 0, 0))
        page.set_rotation(90)
        visual = pdf_loader.visual_bbox(page, [10, 10, 70, 30])
        asset = pdf_loader._save_asset(page, visual, tmp_path, 1, "black-region")
    with Image.open(tmp_path / asset) as image:
        # 反旋转clip会裁到白色背景；黑色区域是可验证的几何事实。
        assert max(ImageStat.Stat(image.convert("RGB")).mean) < 10


def test_hybrid_page_keeps_good_native_uses_mineru_for_formula_and_ocr_only_bad_text(sample_pdf, tmp_path, monkeypatch):
    def inspect_mixed(page, layout_doc, number, identity, root):
        record = inspected(page, layout_doc, number, identity, root)
        if number == 1:
            record["blocks"] = [
                pdf_loader._block(1, "paragraph", "Good native text", [40, 40, 550, 80], "pymupdf", identity, order=0),
                pdf_loader._block(1, "paragraph", "Bad \ufffd\ufffd text", [40, 100, 550, 150], "pymupdf", identity, order=1),
                pdf_loader._block(1, "formula", "Unreliable expression", [40, 200, 550, 230], "pymupdf", identity, order=2),
            ]
            record["native_text"] = "Good native text\nBad \ufffd\ufffd text\nUnreliable expression"
        return record
    monkeypatch.setattr(pdf_loader, "_inspect_page", inspect_mixed)
    monkeypatch.setattr(config, "ENABLE_OCR", True)
    monkeypatch.setattr(pdf_loader.MinerUAdapter, "parse_pages", lambda *a, **kw: {1: [
        pdf_loader._block(1, "formula", "x = y", [40, 200, 550, 230], "mineru", "mineru-hash", order=0, latex="x=y")
    ]})
    ocr_calls = []
    def ocr(image, **kwargs):
        ocr_calls.append(image.size)
        return "Recovered ordinary text"
    monkeypatch.setattr(pdf_loader.pytesseract, "image_to_string", ocr)
    events = []
    records = pdf_loader.extract_pdf_pages(sample_pdf, artifact_dir=tmp_path / "run", event_sink=events.append)
    first = records[0]
    assert len(ocr_calls) == 1
    assert first["text"] == "Good native text\n\nRecovered ordinary text\n\nx = y"
    assert first["engines_used"] == ["mineru", "pymupdf", "tesseract"]
    assert first["quality"]["status"] == "pass"
    assert [b["engine"] for b in first["blocks"] if b["selected"]].count("pymupdf") == 1
    assert [event["page"] for event in events if event["engine"] == "tesseract" and event["event"] == "page_completed"] == [1]


def test_tesseract_only_page_does_not_reocr_healthy_native_regions(sample_pdf, tmp_path, monkeypatch):
    def inspect_ordinary(page, layout_doc, number, identity, root):
        record = inspected(page, layout_doc, number, identity, root)
        if number == 1:
            record["blocks"] = [pdf_loader._block(1, "paragraph", "Good native text", [40, 40, 550, 80], "pymupdf", identity),
                                pdf_loader._block(1, "paragraph", "\ufffd", [40, 100, 550, 150], "pymupdf", identity, order=1)]
            record["native_text"] = "Good native text\n\ufffd"
        return record
    monkeypatch.setattr(pdf_loader, "_inspect_page", inspect_ordinary)
    monkeypatch.setattr(config, "ENABLE_OCR", True)
    calls = []
    monkeypatch.setattr(pdf_loader.pytesseract, "image_to_string", lambda *a, **kw: calls.append(1) or "Recovered ordinary text")
    records = pdf_loader.extract_pdf_pages(sample_pdf, artifact_dir=tmp_path / "run")
    assert calls == [1]
    assert records[0]["text"] == "Good native text\n\nRecovered ordinary text"
    assert records[0]["quality"]["status"] == "pass"


def test_bad_ordinary_region_from_whole_page_mineru_can_use_tesseract(sample_pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_loader, "_inspect_page", lambda *args: inspected(*args, kind="formula", text="Old expression"))
    monkeypatch.setattr(config, "ENABLE_OCR", True)
    monkeypatch.setattr(pdf_loader.MinerUAdapter, "parse_pages", lambda *a, **kw: {1: [
        pdf_loader._block(1, "paragraph", "\ufffd\ufffd", [40, 40, 550, 80], "mineru", "hash", order=0),
        pdf_loader._block(1, "formula", "x=y", [40, 200, 550, 230], "mineru", "hash", order=1, latex="x=y")
    ]})
    monkeypatch.setattr(pdf_loader.pytesseract, "image_to_string", lambda *a, **kw: "Recovered text")
    records = pdf_loader.extract_pdf_pages(sample_pdf, artifact_dir=tmp_path / "run")
    assert records[0]["text"] == "Recovered text\n\nx=y"
    assert records[0]["engines_used"] == ["mineru", "tesseract"]
    assert records[0]["quality"]["status"] == "pass"
