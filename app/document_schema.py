"""混合解析公共契约：ID和文本指纹不包含运行时间。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3
PIPELINE_VERSION = "hybrid-v3.1"


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def make_block(page: int, kind: str, text: str, bbox: list, engine: str, **extra) -> dict:
    # 同页不同区域中的相同数字必须保留，几何和内容共同参与块身份。
    block = {
        "block_id": stable_hash([page, kind, [round(float(v), 3) for v in bbox], engine, text]),
        "page": int(page), "type": kind, "content_text": text,
        "bbox": list(bbox), "engine": engine, "order": 0,
        "quality_status": "pass", "selected": True, "provenance": {},
    }
    block.update(extra)
    return block


@dataclass(frozen=True)
class DocumentSource:
    doc_id: str
    source: str
    source_key: str
    pdf_sha256: str
    pdf_path: str

    def to_dict(self) -> dict:
        return asdict(self)


def pages_label(pages: list[int]) -> str:
    pages = sorted(set(int(page) for page in pages))
    if not pages:
        return "未知"
    if len(pages) > 1 and pages == list(range(pages[0], pages[-1] + 1)):
        return f"{pages[0]}–{pages[-1]}"
    return ", ".join(map(str, pages))
