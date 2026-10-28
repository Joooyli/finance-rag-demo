from app.document_assembler import assemble_document
from app.document_schema import make_block


def page(number, blocks, status="completed"):
    return {"page": number, "geometry": {"width": 600, "height": 800}, "blocks": blocks,
            "status": status, "quality": {"status": "pass"}}


def paragraph(number, text, bbox):
    return make_block(number, "paragraph", text, bbox, "pymupdf", font_size=12)


def table(number, values, bbox, **extra):
    html = "<table><tr><th>项目</th><th>金额</th></tr>" + "".join(
        f"<tr><td>{name}</td><td>{amount}</td></tr>" for name, amount in values
    ) + "</table>"
    return make_block(number, "table", "表格正文", bbox, "mineru", html=html, **extra)


def test_adjacent_paragraphs_merge_with_original_sources():
    first = paragraph(1, "风险管理需要", [50, 690, 540, 780])
    second = paragraph(2, "识别不同风险。", [50, 35, 540, 100])
    logical, merges = assemble_document([page(1, [first]), page(2, [second])])
    assert len(logical) == 1
    assert logical[0]["content_text"] == "风险管理需要识别不同风险。"
    assert logical[0]["pages"] == [1, 2]
    assert [span["page"] for span in logical[0]["source_spans"]] == [1, 2]
    assert merges[0]["decision"] == "accepted"
    assert first["content_text"] == "风险管理需要"


def test_blank_failed_missing_pages_and_heading_do_not_merge():
    first = paragraph(1, "风险管理需要", [50, 690, 540, 780])
    third = paragraph(3, "识别不同风险。", [50, 35, 540, 100])
    for middle in ([page(2, [], "blank")], [page(2, [], "failed")], []):
        logical, merges = assemble_document([page(1, [first]), *middle, page(3, [third])])
        assert len(logical) == 2
        assert not merges
    heading = make_block(2, "heading", "第二章", [50, 20, 540, 45], "pymupdf")
    logical, merges = assemble_document([page(1, [first]), page(2, [heading, paragraph(2, "新的段落", [50, 50, 540, 100])])])
    assert len(logical) == 3
    assert not merges


def test_uncertain_paragraph_font_change_is_recorded_for_review():
    first = paragraph(1, "风险管理需要", [50, 690, 540, 780])
    second = paragraph(2, "识别不同风险。", [50, 35, 540, 100])
    second["font_size"] = 20
    logical, merges = assemble_document([page(1, [first]), page(2, [second])])
    assert len(logical) == 2
    assert merges[0]["decision"] == "needs_review"
    assert all("uncertain_paragraph_continuation" in item["issues"] for item in logical)


def test_continued_table_retains_html_rows_pages_and_header_evidence():
    first = table(28, [("资产", "-1.25%")], [50, 500, 540, 790])
    second = table(29, [("负债", "2.50")], [50, 20, 540, 300], continues_prev=True)
    logical, merges = assemble_document([page(28, [first]), page(29, [second])])
    assert len(logical) == 1
    merged = logical[0]
    assert len(merged["table"]["rows"]) == 3
    assert merged["table"]["rows"][1]["grid"] == ["资产", "-1.25%"]
    assert merged["table"]["rows"][2]["source_spans"][0]["page"] == 29
    assert "-1.25%" in merged["html"] and "2.50" in merged["html"]
    assert merges[0]["repeated_header_rows_removed"] == 1


def test_same_header_does_not_silently_merge_independent_tables():
    first = table(1, [("资产", "10")], [50, 500, 540, 790])
    second = table(2, [("负债", "20")], [50, 20, 540, 300])
    logical, merges = assemble_document([page(1, [first]), page(2, [second])])
    assert len(logical) == 2
    assert merges[0]["decision"] == "needs_review"
    assert all(block["quality_status"] == "needs_review" for block in logical)


def test_continuation_with_incompatible_columns_needs_review():
    first = table(1, [("资产", "10")], [50, 500, 540, 790])
    second = make_block(2, "table", "不同列", [50, 20, 540, 300], "mineru", continues_prev=True,
                        html="<table><tr><th>项目</th><th>金额</th><th>单位</th></tr><tr><td>负债</td><td>20</td><td>万元</td></tr></table>")
    logical, merges = assemble_document([page(1, [first]), page(2, [second])])
    assert len(logical) == 2
    assert not merges[0]["evidence"]["columns_match"]
    assert merges[0]["decision"] == "needs_review"
