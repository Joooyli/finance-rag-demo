from __future__ import annotations
from io import BytesIO
from pathlib import Path
import re
import fitz
from PIL import Image
import pytesseract

from app import config

if config.TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD

def clean_text(text: str) -> str:
    """清理 PDF 提取或 OCR 识别文本中的异常字符和多余空白。"""
    # 将空字符替换为空格，避免直接删除后相邻文字粘连。
    text = text.replace("\x00", " ")
    # 将连续的普通空格和制表符合并为一个空格，保留换行。
    text = re.sub(r"[ \t]+", " ", text)
    # 将三个及以上连续换行压缩为两个，保留段落间隔。
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 去除整个文本首尾的空白字符，包括空格、制表符和换行。
    return text.strip()

def _ocr_image_bytes(image_bytes: bytes) -> str:
    if not config.ENABLE_OCR:
        return ""
    try:
        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        return pytesseract.image_to_string(image, lang=config.OCR_LANG, config="--psm 6")
    except Exception as exc:
        # OCR failure should not prevent extracting the PDF's native text.
        return f""

def extract_pdf_pages(pdf_path: Path, page_sink=None) -> list[dict]:
    """
    Extract selectable text from each PDF page and OCR embedded images (which may
    contain scanned text or image tables). If a page has little native text and
    no embedded image yielded OCR text, optionally OCR a rendered full page.

    ``page_sink`` is called once per PDF page (including pages without any usable
    text) with a dict describing how that page was extracted. It is used to write
    the intermediate parse log; it must never raise, and it does not change the
    returned list, which only contains pages with text.
    """
    pages = []
    doc = fitz.open(pdf_path)
    try:
        for page_index, page in enumerate(doc):
            native = clean_text(page.get_text("text", sort=True))
            image_texts = []
            if config.ENABLE_OCR:
                seen_xrefs = set()
                for image_info in page.get_images(full=True):
                    xref = image_info[0]
                    if xref in seen_xrefs:
                        continue
                    seen_xrefs.add(xref)
                    try:
                        extracted = doc.extract_image(xref)
                        ocr_text = clean_text(_ocr_image_bytes(extracted["image"]))
                        if ocr_text and len(ocr_text) >= 8:
                            image_texts.append(ocr_text)
                    except Exception:
                        continue

            full_page_ocr = ""
            if (config.ENABLE_OCR and config.OCR_LOW_TEXT_PAGES
                    and len(native) < config.MIN_NATIVE_TEXT_CHARS
                    and not image_texts):
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
                    full_page_ocr = clean_text(
                        pytesseract.image_to_string(
                            Image.open(BytesIO(pix.tobytes("png"))),
                            lang=config.OCR_LANG,
                            config="--psm 6",
                        )
                    )
                except Exception:
                    full_page_ocr = ""

            parts = []
            if native:
                parts.append("[PDF原生文本]\n" + native)
            if image_texts:
                parts.append("[页面内嵌图片OCR，表格结构可能有损]\n" + "\n\n".join(image_texts))
            if full_page_ocr:
                parts.append("[整页OCR，表格结构可能有损]\n" + full_page_ocr)

            combined = clean_text("\n\n".join(parts))

            if full_page_ocr:
                mode = "full_page_ocr"
            elif native and image_texts:
                mode = "native+image_ocr"
            elif image_texts:
                mode = "image_ocr"
            elif native:
                mode = "native"
            else:
                mode = "empty"

            page_record = {
                "page": page_index + 1,
                "mode": mode,
                "native_chars": len(native),
                "image_ocr_chars": sum(len(item) for item in image_texts),
                "full_page_ocr_chars": len(full_page_ocr),
                "chars": len(combined),
                "source": pdf_path.name,
                "text": combined,
            }
            if page_sink is not None:
                try:
                    page_sink(page_record)
                except Exception:
                    # Logging/side-channel failures must not break extraction.
                    pass

            if combined:
                pages.append({
                    "page": page_index + 1,
                    "text": combined,
                    "source": pdf_path.name,
                    "mode": mode,
                    "native_chars": len(native),
                    "image_ocr_chars": sum(len(item) for item in image_texts),
                    "full_page_ocr_chars": len(full_page_ocr),
                })
    finally:
        doc.close()
    return pages
