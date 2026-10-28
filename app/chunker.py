from __future__ import annotations
from copy import deepcopy
import hashlib
import re
from app import config
from app import embeddings
from app.document_schema import stable_hash

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
                # 小数点不是句子边界，避免把金融数值切成两段。
                max((m.start() for m in re.finditer(r"\.(?=\s|$)", text) if start + size // 2 <= m.start() < end), default=-1),
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


_MATH = re.compile(r"\$\$[\s\S]*?\$\$|(?<!\\)\$[^$\n]+(?<!\\)\$|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)")
_ATOMIC = re.compile("(?P<formula>" + _MATH.pattern + r")|(?P<number>[-−]?\d+(?:[.,]\d+)*(?:%|％)?)")


def _context(block: dict) -> str:
    parts = []
    section = " / ".join(value for value in block.get("section_path", []) if value)
    if section and section.strip() != block.get("content_text", "").strip():
        parts.append(section)
    for name in ("title", "caption", "unit"):
        value = str(block.get(name) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return "\n".join(parts)


def _with_context(prefix: str, text: str) -> str:
    return (prefix + "\n" + text).strip() if prefix else text.strip()


def _text_pieces(text: str, prefix: str, budget: int) -> list[tuple[str, list[str]]]:
    """按真实token预算寻找边界；内联公式仍保持完整，不靠encode截断。"""
    if not text.strip():
        return []
    parts: list[tuple[str, list[str]]] = []
    remaining = text.strip()
    soft_limit = max(100, int(config.CHUNK_SIZE))
    while remaining:
        if embeddings.count_tokens(_with_context(prefix, remaining)) <= budget and len(remaining) <= soft_limit:
            parts.append((remaining, []))
            break
        low, high = 0, min(len(remaining), soft_limit)
        while low < high:
            middle = (low + high + 1) // 2
            if embeddings.count_tokens(_with_context(prefix, remaining[:middle])) <= budget:
                low = middle
            else:
                high = middle - 1
        if low == 0:
            # 标题上下文本身已超限，完整保留证据，交质量门禁阻断该记录。
            parts.append((remaining, ["context_exceeds_token_budget"]))
            break
        end = low
        boundaries = [m.end() for m in re.finditer(r"\n\n|[。！？!?；;](?:\s*)|\.(?=\s|$)\s*", remaining) if m.end() <= low]
        if boundaries and boundaries[-1] >= low // 2:
            end = boundaries[-1]
        protected = next((match for match in _ATOMIC.finditer(remaining) if match.start() < end < match.end()), None)
        issues: list[str] = []
        if protected:
            if protected.start() > 0:
                end = protected.start()
            elif embeddings.count_tokens(_with_context(prefix, remaining[:protected.end()])) <= budget:
                end = protected.end()
            else:
                end = protected.end()
                issues.append("formula_exceeds_token_budget" if protected.lastgroup == "formula" else "numeric_value_exceeds_token_budget")
        piece = remaining[:end].strip()
        if piece:
            parts.append((piece, issues))
        overlap = min(max(0, int(config.CHUNK_OVERLAP)), end // 3)
        next_start = max(1, end - overlap)
        # 普通文字允许重叠，但重叠起点同样不能落在数学表达式内部。
        for match in _ATOMIC.finditer(remaining):
            if match.start() < next_start < match.end():
                next_start = match.start() if match.start() > 0 else match.end()
                break
        remaining = remaining[next_start:].lstrip()
    return parts


def _table_pieces(block: dict, prefix: str, budget: int) -> list[tuple[str, list[dict], list[str]]]:
    table = block.get("table") or {}
    headers = table.get("headers") or []
    rows = table.get("rows") or []
    header_count = int(table.get("header_rows") or 0)
    if not rows:
        return [(block.get("content_text", ""), block.get("source_spans", []), ["table_structure_missing"])]
    header_text = " | ".join(headers)
    header_sources = [span for row in rows[:header_count] for span in row.get("source_spans", [])]
    data_rows = rows[header_count:]
    if not data_rows:
        return [(header_text, block.get("source_spans", []), [])]
    output: list[tuple[str, list[dict], list[str]]] = []
    current: list[str] = []
    sources: list[dict] = []
    for row in data_rows:
        cells = row.get("grid") or [cell.get("text", "") for cell in row.get("cells", [])]
        line = "；".join(f"{headers[index] if index < len(headers) else f'列{index + 1}'}={value}" for index, value in enumerate(cells))
        candidate = "\n".join([header_text, *current, line]).strip()
        if current and embeddings.count_tokens(_with_context(prefix, candidate)) > budget:
            output.append(("\n".join([header_text, *current]).strip(), header_sources + sources, []))
            current, sources = [], []
        single = "\n".join([header_text, line]).strip()
        if embeddings.count_tokens(_with_context(prefix, single)) > budget:
            # 超长单行不能无声切断列和值的对应关系；保留整行并标待核对。
            output.append((single, header_sources + row.get("source_spans", block.get("source_spans", [])), ["table_row_exceeds_token_budget"]))
            continue
        current.append(line)
        sources.extend(row.get("source_spans", block.get("source_spans", [])))
    if current:
        output.append(("\n".join([header_text, *current]).strip(), header_sources + sources, []))
    return output


def build_document_chunks(
    logical_blocks: list[dict], *, doc_id: str, pdf_sha256: str,
    pipeline_fingerprint: str, source: str, run_id: str,
) -> list[dict]:
    """从文档逻辑块切分，跨页来源随内容保留，所有文本先经过token验证。"""
    budget = embeddings.get_token_budget()
    chunks: list[dict] = []
    for logical in logical_blocks:
        kind = logical["type"]
        # 页眉/页脚是否保留已由统一块清洗决定；切块阶段不重复删除，尊重配置开关。
        text = logical.get("content_text", "").strip()
        if not text and kind != "table":
            # 本期允许仅保留插图资产，不把空占位符或文件路径向量化。
            continue
        prefix = _context(logical)
        if kind == "table":
            pieces = _table_pieces(logical, prefix, budget)
        elif kind == "formula":
            expression = logical.get("latex") or text
            issues = [] if embeddings.count_tokens(_with_context(prefix, expression)) <= budget else ["formula_exceeds_token_budget"]
            pieces = [(expression, logical.get("source_spans", []), issues)]
        else:
            pieces = [(piece, logical.get("source_spans", []), issues) for piece, issues in _text_pieces(text, prefix, budget)]
        for sub_index, (piece, spans, piece_issues) in enumerate(pieces):
            if not piece.strip():
                continue
            embedding_text = _with_context(prefix, piece)
            tokens = embeddings.count_tokens(embedding_text)
            issues = sorted(set(logical.get("issues", []) + piece_issues))
            if tokens > budget and not piece_issues:
                issues.append("embedding_token_budget_exceeded")
            unique_spans = []
            seen = set()
            for span in spans:
                key = stable_hash(span)
                if key not in seen:
                    seen.add(key)
                    unique_spans.append(deepcopy(span))
            # 行组的来源比父表范围更精确；重复表头作为上下文仍保留其来源页。
            pages = sorted({int(span["page"]) for span in unique_spans} or set(logical["pages"]))
            block_ids = list(dict.fromkeys(span["block_id"] for span in unique_spans)) or logical["source_block_ids"]
            digest = hashlib.sha256(embedding_text.encode("utf-8")).hexdigest()
            chunk_id = stable_hash([doc_id, pdf_sha256, pipeline_fingerprint, block_ids, sub_index, digest])
            chunks.append({
                "schema_version": 3, "chunk_id": chunk_id, "doc_id": doc_id,
                "pdf_sha256": pdf_sha256, "pipeline_fingerprint": pipeline_fingerprint,
                "source": source, "run_id": run_id, "chunk_index": len(chunks),
                "content_type": kind, "parent_id": logical["logical_block_id"],
                "pages": pages, "page_start": pages[0], "page_end": pages[-1], "page": pages[0],
                "text": piece, "embedding_text": embedding_text,
                "embedding_tokens": tokens, "embedding_text_sha256": digest,
                "source_spans": unique_spans, "block_ids": block_ids,
                "merge_ids": list(logical.get("merge_ids", [])),
                "quality_status": "needs_review" if issues or logical.get("quality_status") != "pass" else "pass",
                "issues": issues, "engine": logical.get("engine", "unknown"),
                "artifact_ref": f"chunks.jsonl#{chunk_id}",
            })
    return chunks
