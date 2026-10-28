from app.text_utils import normalize_markdown, parse_html_table, sanitize_text, slice_safely


def test_sanitize_replaces_surrogates_and_keeps_length():
    raw = "正常文本\ud800中间\udc00结束"
    cleaned = sanitize_text(raw)
    assert len(cleaned) == len(raw)  # 1:1 替换，pos 索引不偏移
    assert "\ud800" not in cleaned
    assert cleaned.encode("utf-8")  # 可安全编码为 UTF-8


def test_normalize_removes_picture_markers_and_blank_lines():
    raw = (
        "<!-- Start of picture text -->\n"
        "上<br>=<br>FRM\n"
        "<!-- End of picture text -->\n\n\n\n"
        "**==> picture [10 x 10] <==**\n"
        "# 标题\n\n正文  \n"
    )
    text = normalize_markdown(raw)
    assert "Start of picture text" not in text
    assert "picture [" not in text
    assert "\n\n\n" not in text
    assert "# 标题" in text
    assert text.endswith("正文")


def test_slice_safely_handles_out_of_range():
    assert slice_safely("abcdef", 2, 4) == "cd"
    assert slice_safely("abcdef", 4, 2) == ""
    assert slice_safely("abcdef", -5, 100) == "abcdef"
    assert slice_safely("", 0, 10) == ""


def test_html_table_preserves_spans_math_and_financial_values():
    html = "<table><tr><th>项目</th><th colspan='2'>收益</th></tr><tr><td rowspan='2'>组合A</td><td>-1.25%</td><td><eq>x_1^2</eq></td></tr><tr><td>2.50</td><td>万元</td></tr></table>"
    parsed = parse_html_table(html)
    assert parsed["html"] == html
    assert parsed["column_count"] == 3
    assert parsed["rows"][1]["cells"][0]["rowspan"] == 2
    assert parsed["rows"][2]["grid"] == ["组合A", "2.50", "万元"]
    assert parsed["rows"][1]["grid"] == ["组合A", "-1.25%", "x_1^2"]
    assert not parsed["issues"]


def test_invalid_html_span_is_not_treated_as_valid_table():
    parsed = parse_html_table("<table><tr><td colspan='bad'>金额</td></tr></table>")
    assert "invalid_table_span" in parsed["issues"]


def test_missing_table_cells_and_untranscribed_images_require_review():
    parsed = parse_html_table("<table><tr><th>项目</th><th>金额</th></tr><tr><td><img src='formula.png'></td></tr></table>")
    assert "incomplete_table_row" in parsed["issues"]
    assert "untranscribed_table_image" in parsed["issues"]
