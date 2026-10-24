from app.text_utils import normalize_markdown, sanitize_text, slice_safely


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
