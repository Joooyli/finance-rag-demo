"""统一块清洗规则测试：不启动模型，重点验证误删保护和来源证据。"""
from copy import deepcopy
import logging

import pytest

from app import config
from app.document_schema import make_block
from app.header_filter import clean_document_blocks


AD = "扫码领取学习资料，请勿私自传播。"


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    for name, value in {"HEADER_REMOVE": True, "FOOTER_REMOVE": False,
                        "HEADER_FILTER_TOP_RATIO": .12, "HEADER_REPEAT_MIN_PAGES": 3,
                        "HEADER_REPEAT_MIN_RATIO": .6, "HEADER_REPEAT_POSITION_TOLERANCE": .02,
                        "HEADER_AD_PATTERNS": ()}.items():
        monkeypatch.setattr(config, name, value, raising=False)


def block(number, text, *, kind="paragraph", bbox=None, engine="pymupdf", **extra):
    return make_block(number, kind, text, bbox or [40, 20, 550, 45], engine, **extra)


def page(number, blocks):
    return {"page": number, "geometry": {"width": 600, "height": 800}, "blocks": blocks,
            "native_text": AD + "\n原始正文", "raw_text": "原始Markdown", "unresolved_issues": []}


def selected_texts(pages):
    return [b["content_text"] for p in pages for b in p["blocks"] if b.get("selected", True)]


def test_repeated_header_is_removed_across_engines_but_same_words_in_body_remain(caplog):
    pages = [page(number, [block(number, AD, engine=engine),
                           block(number, AD, bbox=[40, 200, 550, 225], engine=engine)])
             for number, engine in enumerate(("pymupdf", "mineru", "tesseract"), 1)]
    events = []
    with caplog.at_level(logging.INFO):
        result = clean_document_blocks(pages, event_sink=events.append)
    assert result is pages
    assert selected_texts(pages) == [AD, AD, AD]
    assert all(p["blocks"][0]["excluded_reason"] == "advertising_header" for p in pages)
    assert all(p["cleaning"]["excluded"] == 1 for p in pages)
    assert all(p["native_text"] == AD + "\n原始正文" and p["raw_text"] == "原始Markdown" for p in pages)
    assert len([e for e in events if e["event"] == "header_excluded"]) == 3
    assert "cleaning_completed page=3" in caplog.text


def test_top_position_alone_and_repeated_heading_table_formula_are_protected():
    pages = [page(number, [block(number, "风险管理章节", kind="heading"),
                           block(number, "资产 100 负债 80", kind="table", bbox=[40, 50, 550, 90]),
                           block(number, "x+y=1", kind="formula", bbox=[40, 60, 550, 90])])
             for number in range(1, 4)]
    clean_document_blocks(pages)
    assert len(selected_texts(pages)) == 9
    assert not any(p["cleaning"]["excluded"] for p in pages)


def test_repeated_text_at_different_top_positions_is_not_inferred_as_header():
    pages = [page(number, [block(number, AD, bbox=[40, y, 550, y + 15])])
             for number, y in enumerate((10, 40, 75), 1)]
    clean_document_blocks(pages)
    assert selected_texts(pages) == [AD] * 3


def test_same_text_outside_accepted_repeat_cluster_is_preserved():
    pages = [page(number, [block(number, AD, bbox=[40, 10 if number < 4 else 70, 550, 25 if number < 4 else 85])])
             for number in range(1, 5)]
    clean_document_blocks(pages)
    assert selected_texts(pages) == [AD]
    assert pages[-1]["blocks"][0]["selected"] is True


def test_variable_financial_numbers_are_not_generalized_for_repeat_detection():
    pages = [page(number, [block(number, f"资产金额 {number}.25 万元")]) for number in range(1, 4)]
    clean_document_blocks(pages)
    assert len(selected_texts(pages)) == 3


def test_minimum_pages_and_coverage_are_both_required():
    pages = [page(number, [block(number, AD)]) for number in range(1, 3)]
    clean_document_blocks(pages)
    assert selected_texts(pages) == [AD] * 2
    pages = [page(number, [block(number, AD if number <= 3 else f"正常文本{number}")]) for number in range(1, 7)]
    clean_document_blocks(pages)
    assert len(selected_texts(pages)) == 6


def test_configured_exact_ad_can_remove_one_page_header_and_figure(monkeypatch):
    monkeypatch.setattr(config, "HEADER_AD_PATTERNS", (r"^扫码领取学习资料，请勿私自传播。$",))
    pages = [page(1, [block(1, AD, kind="heading"), block(1, AD, kind="figure", bbox=[40, 50, 550, 80])])]
    clean_document_blocks(pages)
    assert selected_texts(pages) == []
    assert pages[0]["header_only"] is True
    assert pages[0]["cleaning"]["excluded"] == 2


def test_structured_content_matching_ad_is_reviewed_not_deleted(monkeypatch):
    monkeypatch.setattr(config, "HEADER_AD_PATTERNS", (r"^扫码领取学习资料，请勿私自传播。$",))
    pages = [page(1, [block(1, AD, kind="table"), block(1, AD, kind="formula")])]
    clean_document_blocks(pages)
    assert selected_texts(pages) == [AD, AD]
    assert all(b["quality_status"] == "needs_review" for b in pages[0]["blocks"])
    assert len(pages[0]["unresolved_issues"]) == 2


@pytest.mark.parametrize("kind", ["table", "formula"])
def test_structured_block_crossing_header_band_with_ad_cannot_pass_quality(monkeypatch, kind):
    monkeypatch.setattr(config, "HEADER_AD_PATTERNS", (r"^扫码领取学习资料，请勿私自传播。$",))
    original = block(1, AD + "\n金融正文 -12.50%", kind=kind, bbox=[40, 8, 500, 200])
    pages = [page(1, [original])]
    clean_document_blocks(pages)
    assert original["selected"] is True
    assert original["quality_status"] == "needs_review"
    assert original["content_text"] == AD + "\n金融正文 -12.50%"
    assert pages[0]["unresolved_issues"][0]["code"] == "advertising_header_inside_structure"


def test_structured_block_crossing_top_detects_ad_from_other_pages_repeat():
    pages = [page(number, [block(number, AD)]) for number in range(1, 4)]
    structured = block(4, AD + "\n资产 金额 12.50", kind="table", bbox=[40, 8, 500, 200])
    pages.append(page(4, [structured]))
    clean_document_blocks(pages)
    assert structured["selected"] is True and structured["quality_status"] == "needs_review"


def test_classified_header_and_footer_obey_separate_switches(monkeypatch):
    pages = [page(1, [block(1, "广告", kind="header", bbox=[40, 300, 550, 320]),
                      block(1, "脚注", kind="footer", bbox=[40, 750, 550, 770])])]
    clean_document_blocks(pages)
    assert selected_texts(pages) == ["脚注"]
    monkeypatch.setattr(config, "HEADER_REMOVE", False)
    monkeypatch.setattr(config, "FOOTER_REMOVE", True)
    pages = [page(1, [block(1, "广告", kind="header"), block(1, "1", kind="page_number", bbox=[40, 750, 550, 770])])]
    clean_document_blocks(pages)
    assert selected_texts(pages) == ["广告"]


def test_disabled_header_removal_preserves_even_explicit_ad_and_repetition(monkeypatch):
    monkeypatch.setattr(config, "HEADER_REMOVE", False)
    monkeypatch.setattr(config, "HEADER_AD_PATTERNS", (r"扫码.*",))
    pages = [page(number, [block(number, AD), block(number, AD, kind="header")]) for number in range(1, 4)]
    clean_document_blocks(pages)
    assert len(selected_texts(pages)) == 6


def test_mixed_block_is_split_using_exact_line_offsets_and_visual_bboxes(monkeypatch):
    monkeypatch.setattr(config, "HEADER_AD_PATTERNS", (r"^扫码领取学习资料，请勿私自传播。$",))
    body = "金融正文数值 -12.50% 不得丢失。"
    text = AD + "\n\n" + body
    original = block(1, text, bbox=[40, 20, 550, 200], lines=[
        {"text": AD, "bbox": [40, 20, 550, 45], "char_range": [0, len(AD)]},
        {"text": body, "bbox": [40, 170, 550, 200], "char_range": [len(AD) + 2, len(text)]}],
        title=AD, caption=AD + "\n财务说明", section_path=[AD, "金融风险"], provenance={"raw_pos": [100, 200]})
    pages = [page(1, [original])]
    clean_document_blocks(pages)
    assert original["content_text"] == text and original["selected"] is False
    assert original["excluded_reason"] == "advertising_header_split"
    child = next(b for b in pages[0]["blocks"] if b.get("selected", True))
    assert child["block_id"] != original["block_id"]
    assert child["content_text"] == body and child["bbox"] == [40, 170, 550, 200]
    assert child["lines"][0]["char_range"] == [0, len(body)]
    assert child["provenance"]["source_char_range"] == [len(AD) + 2, len(text)]
    assert child["provenance"]["source_block_id"] == original["block_id"]
    assert "raw_pos" not in child["provenance"]
    assert child["title"] == "" and child["caption"] == "财务说明" and child["section_path"] == ["金融风险"]


def test_misclassified_header_with_body_lines_splits_and_reclassifies_retained_body():
    body = "正文金融数值 -12.50% 必须保留。"
    text = AD + "\n" + body
    original = block(1, text, kind="header", bbox=[40, 8, 500, 200], lines=[
        {"text": AD, "bbox": [40, 8, 500, 30], "char_range": [0, len(AD)]},
        {"text": body, "bbox": [40, 150, 500, 200], "char_range": [len(AD) + 1, len(text)]}],
        title=AD, caption=AD + "\n财务说明", section_path=[AD, "风险管理"])
    pages = [page(1, [original])]
    clean_document_blocks(pages)
    assert original["selected"] is False and original["content_text"] == text
    child = next(item for item in pages[0]["blocks"] if item.get("selected", True))
    assert child["content_text"] == body and child["type"] == "paragraph"
    assert child["provenance"]["source_block_type"] == "header"
    assert child["provenance"]["source_block_id"] == original["block_id"]
    assert child["title"] == "" and child["caption"] == "财务说明"
    assert child["section_path"] == ["风险管理"]
    assert child["quality_status"] == "pass"


@pytest.mark.parametrize("text", [AD + "\n正文金额12.50。", AD])
def test_large_header_without_reliable_lines_is_retained_for_review(text):
    original = block(1, text, kind="header", bbox=[40, 8, 500, 200])
    pages = [page(1, [original])]
    clean_document_blocks(pages)
    assert original["selected"] is True
    assert original["content_text"] == text and original["quality_status"] == "needs_review"
    assert pages[0]["unresolved_issues"][0]["code"] == "advertising_header_not_separable"


@pytest.mark.parametrize("lines", [None, [{"text": AD, "bbox": [40, 20, 550, 45], "char_range": [1, 10]}]])
def test_unseparable_mixed_block_preserves_body_and_marks_review(monkeypatch, lines):
    monkeypatch.setattr(config, "HEADER_AD_PATTERNS", (r"^扫码领取学习资料，请勿私自传播。$",))
    text = AD + "\n正常正文。"
    original = block(1, text, bbox=[40, 20, 550, 300], **({"lines": lines} if lines else {}))
    pages = [page(1, [original])]
    clean_document_blocks(pages)
    assert selected_texts(pages) == [text]
    assert original["quality_status"] == "needs_review"
    assert pages[0]["unresolved_issues"][0]["code"] == "advertising_header_not_separable"


def test_mixed_lines_can_use_document_repeat_evidence_without_configured_pattern():
    pages = []
    for number in range(1, 4):
        body = f"本页独立正文{number}。"
        pages.append(page(number, [block(number, AD + "\n" + body, bbox=[40, 20, 550, 200], lines=[
            {"text": AD, "bbox": [40, 20, 550, 45]}, {"text": body, "bbox": [40, 170, 550, 200]}])]))
    clean_document_blocks(pages)
    assert selected_texts(pages) == ["本页独立正文1。", "本页独立正文2。", "本页独立正文3。"]


def test_selected_false_evidence_is_not_reactivated_and_cleanup_is_idempotent():
    pages = [page(1, [block(1, AD, kind="header"), block(1, "原生旧候选", selected=False)])]
    events = []
    clean_document_blocks(pages, event_sink=events.append)
    snapshot, event_count = deepcopy(pages), len(events)
    clean_document_blocks(pages, event_sink=events.append)
    assert pages == snapshot and len(events) == event_count
    assert selected_texts(pages) == []


def test_repeated_ad_context_is_removed_even_from_earlier_page_body_metadata():
    pages = [page(number, [block(number, AD), block(number, "正文", bbox=[40, 200, 550, 230], title=AD)])
             for number in range(1, 4)]
    clean_document_blocks(pages)
    assert all(p["blocks"][1]["title"] == "" for p in pages)


def test_bad_regex_or_settings_fails_instead_of_silently_disabling_filter(monkeypatch):
    monkeypatch.setattr(config, "HEADER_AD_PATTERNS", ("[",))
    with pytest.raises(Exception):
        clean_document_blocks([page(1, [])])
    monkeypatch.setattr(config, "HEADER_AD_PATTERNS", ())
    monkeypatch.setattr(config, "HEADER_FILTER_TOP_RATIO", .9)
    with pytest.raises(ValueError, match="参数"):
        clean_document_blocks([page(1, [])])
