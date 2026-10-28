"""所有用户读取都经过活动快照；暂存和历史向量不会泄漏到UI或检索。"""
from __future__ import annotations

import json
import math
from app import config
from app.document_schema import pages_label, text_hash
from app.index_registry import get_active_snapshot


def get_collection(embedding_signature: str | None = None):
    # 延迟导入，单纯解析和契约测试不需要启动Chroma。
    import chromadb
    from chromadb.config import Settings
    config.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(config.CHROMA_DIR), settings=Settings(anonymized_telemetry=False))
    desired = {"hnsw:space": "cosine"}
    if embedding_signature:
        desired["embedding_signature"] = embedding_signature
    collection = client.get_or_create_collection(name=config.COLLECTION_NAME, metadata=desired)
    if embedding_signature:
        metadata = dict(collection.metadata or {})
        old = metadata.get("embedding_signature")
        if old != embedding_signature:
            if collection.count() or old:
                raise ValueError("目标集合Embedding身份不匹配，请使用独立新集合")
            # Chroma0.6禁止modify中的hnsw:space，即使值没变。仅空集合可安全重建。
            client.delete_collection(config.COLLECTION_NAME)
            collection = client.create_collection(name=config.COLLECTION_NAME, metadata=desired)
    return collection


def upsert_chunks(ids, documents, metadatas, embeddings, *, embedding_signature=None):
    if not (len(ids) == len(documents) == len(metadatas) == len(embeddings)):
        raise ValueError("向量记录列数不一致")
    if any(not vector or any(not math.isfinite(float(x)) for x in vector) for vector in embeddings):
        raise ValueError("向量包含空值或非有限数")
    collection = get_collection(embedding_signature)
    collection.upsert(ids=ids, documents=documents, metadatas=metadatas, embeddings=embeddings)
    return collection.count()


def _active_where(snapshot: list[dict]) -> dict:
    return {"index_id": {"$in": [row["index_id"] for row in snapshot]}}


def _record(record_id: str, text: str, metadata: dict) -> dict:
    pages = json.loads(metadata.get("pages_json", "[]"))
    return {**metadata, "metadata": metadata, "vector_record_id": record_id,
            "chunk_id": metadata.get("chunk_id", ""), "text": text or "",
            "source_spans": json.loads(metadata.get("source_spans_json", "[]")),
            "chars": len(text or ""), "pages": pages, "page_label": pages_label(pages),
            "page": metadata.get("page_start", -1), "source": metadata.get("source", "unknown")}


def list_active_chunks(source: str | None = None, page: int | None = None, q: str | None = None) -> list[dict]:
    snapshot = get_active_snapshot()
    if not snapshot:
        return []
    result = get_collection().get(where=_active_where(snapshot), include=["documents", "metadatas"])
    rows = [_record(i, text, meta or {}) for i, text, meta in zip(result["ids"], result["documents"], result["metadatas"])]
    # 精确来源过滤发生在分页前，不能把页范围空洞当成命中。
    return sorted((r for r in rows if (source is None or r["source"] == source)
                   and (page is None or page in r["pages"]) and (not q or q in r["text"])),
                  key=lambda r: (r.get("doc_id", ""), r.get("chunk_index", 0)))


def get_active_chunk(vector_record_id: str) -> dict | None:
    # 详情也使用同一快照过滤，不能按任意ID直接读出staged记录。
    snapshot = get_active_snapshot()
    if not snapshot:
        return None
    result = get_collection().get(ids=[vector_record_id], where=_active_where(snapshot), include=["documents", "metadatas"])
    return _record(result["ids"][0], result["documents"][0], result["metadatas"][0] or {}) if result["ids"] else None


def search_chunks(query_embedding, top_k: int):
    snapshot = get_active_snapshot()
    if not snapshot:
        return []
    count = sum(len(json.loads(row["ids_json"])) for row in snapshot)
    if not count:
        return []
    result = get_collection().query(query_embeddings=[query_embedding], n_results=min(top_k, count),
                                    where=_active_where(snapshot), include=["documents", "metadatas", "distances"])
    hits = []
    for identity, text, metadata, distance in zip(result["ids"][0], result["documents"][0], result["metadatas"][0], result["distances"][0]):
        hit = _record(identity, text, metadata or {})
        hit.update(distance=float(distance), similarity=round(1.0 - float(distance), 4))
        hits.append(hit)
    return hits


def verify_index(index_id: str, ids: list[str], *, dimension: int) -> None:
    result = get_collection().get(where={"index_id": index_id}, include=["documents", "metadatas", "embeddings"])
    if set(result["ids"]) != set(ids):
        raise ValueError("暂存索引成员清单不完整")
    for text, meta, vector in zip(result["documents"], result["metadatas"], result["embeddings"]):
        if text_hash(text) != meta.get("embedding_text_sha256") or len(vector) != dimension:
            raise ValueError("暂存向量读回校验失败")


def collection_count() -> int:
    return len(list_active_chunks())
