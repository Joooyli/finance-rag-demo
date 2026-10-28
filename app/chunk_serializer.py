"""统一将已审阅ChunkRecord映射为Chroma可接受的标量metadata。"""
from __future__ import annotations

import json
from app.document_schema import SCHEMA_VERSION, stable_hash


def serialize_chunk(record: dict, *, index_id: str, parse_revision: str,
                    embedding_signature: str) -> tuple[str, str, dict]:
    record_id = stable_hash([index_id, record["chunk_id"]])
    keys = ("doc_id", "source", "pdf_sha256", "run_id", "pipeline_fingerprint",
            "chunk_id", "chunk_index", "page_start", "page_end", "content_type",
            "parent_id", "engine", "embedding_text_sha256", "embedding_tokens", "artifact_ref")
    metadata = {k: record[k] for k in keys if record.get(k) is not None}
    metadata.update({"schema_version": SCHEMA_VERSION, "vector_record_id": record_id,
                     "parse_revision": parse_revision, "index_id": index_id,
                     "embedding_signature": embedding_signature, "quality_status": "pass",
                     "review_status": "approved", "page": record["page_start"],
                     "pages_json": json.dumps(record["pages"]),
                     "source_spans_json": json.dumps(record["source_spans"], ensure_ascii=False)})
    # list/dict/None不能写进Chroma 0.6 metadata；完整来源保存在审阅产物。
    if any(not isinstance(v, (str, int, float, bool)) for v in metadata.values()):
        raise ValueError("Chroma metadata必须为标量")
    return record_id, record["embedding_text"], metadata
