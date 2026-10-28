from __future__ import annotations

import json
import uuid

import pytest

from app import config, vector_store
from app.document_schema import text_hash
from app.index_registry import activate_index, register_document, stage_index


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INDEX_REGISTRY_PATH", tmp_path / "registry.sqlite3")
    monkeypatch.setattr(config, "CHROMA_DIR", tmp_path / "chroma")
    monkeypatch.setattr(config, "COLLECTION_NAME", "test_vector_" + uuid.uuid4().hex[:12])
    return register_document("source:test", "fixture.pdf")


def metadata(index, logical, text, pages):
    return {"index_id": index, "chunk_id": logical, "source": "fixture.pdf", "pages_json": json.dumps(pages),
            "page_start": pages[0], "page_end": pages[-1], "chunk_index": 0,
            "embedding_text_sha256": text_hash(text)}


def test_exact_pages_filtering_and_staged_detail_isolation(store):
    vector_store.upsert_chunks(["active-record", "staged-record"], ["跨页来源", "暂存"],
        [metadata("active-index", "logical1", "跨页来源", [1, 3]), metadata("staged-index", "logical2", "暂存", [2])],
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], embedding_signature="model-v1")
    stage_index("active-index", store, "run1", "model-v1", ["active-record"])
    stage_index("staged-index", store, "run2", "model-v1", ["staged-record"])
    activate_index("active-index")
    assert vector_store.list_active_chunks(page=2) == []
    assert vector_store.list_active_chunks(page=3)[0]["pages"] == [1, 3]
    assert vector_store.get_active_chunk("staged-record") is None
    assert vector_store.get_active_chunk("active-record")["chunk_id"] == "logical1"
    hits = vector_store.search_chunks([1.0, 0.0, 0.0], 10)
    assert len(hits) == 1
    assert hits[0]["vector_record_id"] == "active-record"
    assert hits[0]["page_label"] == "1, 3"


def test_collection_rejects_mixed_embedding_identity(store):
    vector_store.upsert_chunks(["v1"], ["text"], [metadata("index", "logical", "text", [1])],
                               [[1.0, 0.0, 0.0]], embedding_signature="model-v1")
    with pytest.raises(ValueError, match="Embedding身份"):
        vector_store.get_collection("model-v2")


def test_readback_verifies_complete_members_text_hash_and_dimensions(store):
    vector_store.upsert_chunks(["v1"], ["text"], [metadata("index", "logical", "text", [1])],
                               [[1.0, 0.0, 0.0]], embedding_signature="model-v1")
    vector_store.verify_index("index", ["v1"], dimension=3)
    with pytest.raises(ValueError, match="成员清单"):
        vector_store.verify_index("index", ["v1", "missing"], dimension=3)
    with pytest.raises(ValueError, match="读回校验"):
        vector_store.verify_index("index", ["v1"], dimension=5)
    vector_store.get_collection().update(ids=["v1"], documents=["changed"], embeddings=[[1.0, 0.0, 0.0]])
    with pytest.raises(ValueError, match="读回校验"):
        vector_store.verify_index("index", ["v1"], dimension=3)


def test_invalid_vectors_do_not_initialize_collection(store):
    with pytest.raises(ValueError, match="非有限"):
        vector_store.upsert_chunks(["v1"], ["text"], [{}], [[float("nan")]])
