"""文本清洗工具：供解析器与日志写入共用。

设计原则：
1. ``sanitize_text`` 只做 **1:1 替换**，不改变字符串长度，保证 ``page_boxes`` 的
   ``pos`` 区间始终能索引到原文。
2. ``normalize_markdown`` 会改变长度，只用于生成给切块使用的正文 ``text``，
   不得用 ``pos`` 去索引它的结果。
"""

from __future__ import annotations

import re

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
