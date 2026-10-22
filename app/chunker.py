from __future__ import annotations
import re
from app import config

def split_text(text: str, chunk_size: int | None = None,
               overlap: int | None = None) -> list[str]:
    """Simple character-based overlapping chunker, intended for learning."""
    size = chunk_size or config.CHUNK_SIZE
    overlap = config.CHUNK_OVERLAP if overlap is None else overlap
    if size < 100:
        raise ValueError("CHUNK_SIZE should be at least 100 characters")
    if overlap < 0 or overlap >= size:
        raise ValueError("CHUNK_OVERLAP must be >= 0 and < CHUNK_SIZE")

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        # Prefer cutting at a paragraph/newline or Chinese/English sentence boundary.
        if end < len(text):
            candidates = [
                text.rfind("\n\n", start + size // 2, end),
                text.rfind("\n", start + size // 2, end),
                text.rfind("。", start + size // 2, end),
                text.rfind("；", start + size // 2, end),
                text.rfind(".", start + size // 2, end),
            ]
            boundary = max(candidates)
            if boundary > start:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks
