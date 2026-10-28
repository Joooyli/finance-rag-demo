"""文本清洗工具：供解析器与日志写入共用。

设计原则：
1. ``sanitize_text`` 只做 **1:1 替换**，不改变字符串长度，保证 ``page_boxes`` 的
   ``pos`` 区间始终能索引到原文。
2. ``normalize_markdown`` 会改变长度，只用于生成给切块使用的正文 ``text``，
   不得用 ``pos`` 去索引它的结果。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# 孤立代理字符：PyMuPDF 在个别页面会输出（本 PDF 的第 17/18/23/31 页共 121 个），
# 直接 json.dumps + utf-8 编码会抛 UnicodeEncodeError。
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

# PyMuPDF 对 OCR 出来的图片文字会用 HTML 注释包裹。
PICTURE_TEXT_START = "<!-- Start of picture text -->"
PICTURE_TEXT_END = "<!-- End of picture text -->"

# 未写图片时也可能残留的图片占位符，整行删除。
_PICTURE_PLACEHOLDER_RE = re.compile(r"^[ \t]*\*\*==> picture .*?<==\*\*[ \t]*$", re.MULTILINE)

# 零宽字符与 BOM，会污染 embedding 且肉眼不可见。
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)

# 行尾空白
_TRAILING_SPACE_RE = re.compile(r"[ \t]+$", re.MULTILINE)

# 3 个以上连续换行压成 2 个（保留段落分隔）
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def sanitize_text(text: str) -> str:
    """把孤立代理字符替换为 U+FFFD，长度不变，可安全写入 JSON/UTF-8。"""
    if not text:
        return ""
    return _SURROGATE_RE.sub("\ufffd", text)


def normalize_markdown(raw: str) -> str:
    """把 markdown 规范化为可切块正文。

    - 去掉 ``<!-- Start of picture text -->`` 之类的包裹标记，保留内部文字；
    - 删除图片占位符整行；
    - 去掉零宽字符、行尾空白；
    - 压缩多余空行。
    """
    text = sanitize_text(raw)
    text = text.replace(PICTURE_TEXT_START, "").replace(PICTURE_TEXT_END, "")
    text = _PICTURE_PLACEHOLDER_RE.sub("", text)
    text = text.translate(_ZERO_WIDTH)
    text = _TRAILING_SPACE_RE.sub("", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def slice_safely(text: str, start: int, stop: int) -> str:
    """按 pos 区间安全切片，越界或反向区间都返回空串。"""
    if not text:
        return ""
    start = max(0, min(int(start), len(text)))
    stop = max(start, min(int(stop), len(text)))
    return text[start:stop]


class _TableParser(HTMLParser):
    """保留行列跨度的轻量HTML适配器，不执行HTML，也不改写数值。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self.row: dict | None = None
        self.cell: dict | None = None
        self.depth = 0
        self.issues: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "table":
            self.depth += 1
            if self.depth > 1:
                self.issues.append("nested_table_requires_review")
        if self.depth > 1:
            return
        if tag == "tr":
            if self.row is not None:
                self.issues.append("unclosed_table_row")
            self.row = {"cells": []}
        elif tag in {"td", "th"} and self.row is not None:
            spans = {}
            for key in ("rowspan", "colspan"):
                try:
                    value = int(attributes.get(key) or 1)
                    if value < 1 or value > 1000:
                        raise ValueError(key)
                    spans[key] = value
                except ValueError:
                    spans[key] = 1
                    self.issues.append("invalid_table_span")
            self.cell = {"text": "", "header": tag == "th", **spans}
        elif tag in {"br", "p"} and self.cell is not None:
            self.cell["text"] += "\n"
        elif tag == "img" and self.cell is not None:
            # 仅保留真实alt转录；图片路径/base64没有检索语义。
            self.cell["text"] += attributes.get("alt") or ""
            if not attributes.get("alt"):
                self.issues.append("untranscribed_table_image")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self.depth = max(0, self.depth - 1)
            return
        if self.depth > 1:
            return
        if tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.cell["text"] = re.sub(r"[ \t\r\n]+", " ", self.cell["text"]).strip()
            self.row["cells"].append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data: str) -> None:
        if self.cell is not None and self.depth <= 1:
            self.cell["text"] += sanitize_text(data)


def parse_html_table(html: str) -> dict:
    """返回原HTML、结构行和展开网格；网格保留rowspan值的列归属。"""
    parser = _TableParser()
    parser.feed(html or "")
    parser.close()
    if parser.row is not None or parser.cell is not None or parser.depth:
        parser.issues.append("unclosed_table_markup")
    if not parser.rows:
        parser.issues.append("table_rows_missing")

    occupied: dict[tuple[int, int], str] = {}
    column_count = 0
    for row_index, row in enumerate(parser.rows):
        column = 0
        for cell in row["cells"]:
            while (row_index, column) in occupied:
                column += 1
            cell["column"] = column
            for row_offset in range(cell["rowspan"]):
                for column_offset in range(cell["colspan"]):
                    key = (row_index + row_offset, column + column_offset)
                    if key in occupied:
                        parser.issues.append("overlapping_table_span")
                    occupied[key] = cell["text"]
            column += cell["colspan"]
        width = max((c + 1 for r, c in occupied if r == row_index), default=0)
        column_count = max(column_count, width)
    for row_index, row in enumerate(parser.rows):
        row["grid"] = [occupied.get((row_index, c), "") for c in range(column_count)]
        if any((row_index, c) not in occupied for c in range(column_count)):
            parser.issues.append("incomplete_table_row")

    header_rows = 0
    for row in parser.rows:
        if row["cells"] and all(cell["header"] for cell in row["cells"]):
            header_rows += 1
        else:
            break
    if not header_rows and len(parser.rows) > 1:
        # MinerU经常把表头输出为td：仅在首行无数字、后续数据含数字时保守推断。
        first = parser.rows[0]["grid"]
        later = " ".join(value for row in parser.rows[1:] for value in row["grid"])
        if first and not any(re.search(r"\d", value) for value in first) and re.search(r"\d", later):
            header_rows = 1
    headers = []
    for column in range(column_count):
        values: list[str] = []
        for row in parser.rows[:header_rows]:
            value = row["grid"][column]
            if value and (not values or values[-1] != value):
                values.append(value)
        headers.append(" / ".join(values) or f"列{column + 1}")
    if any(r >= len(parser.rows) for r, _ in occupied):
        parser.issues.append("rowspan_beyond_table")
    return {
        "html": html,
        "rows": parser.rows,
        "column_count": column_count,
        "header_rows": header_rows,
        "headers": headers,
        "issues": sorted(set(parser.issues)),
    }
