"""检索契约测试不依赖用户的真实索引或模型缓存。"""
import pytest
from app import rag_pipeline


def test_no_active_index_does_not_load_model(monkeypatch):
    monkeypatch.setattr(rag_pipeline, "get_active_snapshot", lambda: [])
    monkeypatch.setattr(rag_pipeline.embeddings, "embed_query", lambda _: pytest.fail("不应加载模型"))
    assert rag_pipeline.retrieve("期权价值受到什么影响") == []


def test_query_embedding_identity_must_match(monkeypatch):
    monkeypatch.setattr(rag_pipeline, "get_active_snapshot", lambda: [{"signature": "old"}])
    monkeypatch.setattr(rag_pipeline.embeddings, "get_embedding_signature", lambda: {"signature": "new"})
    with pytest.raises(ValueError, match="Embedding"):
        rag_pipeline.retrieve("市场风险")


def test_context_skips_large_block_and_reports_only_sent_sources(monkeypatch):
    monkeypatch.setattr(rag_pipeline.config, "MAX_CONTEXT_CHARS", 100)
    hits = [{"source": "a.pdf", "pages": [28, 29], "text": "x" * 200},
            {"source": "a.pdf", "pages": [30], "text": "有效的小片段"}]
    context, sent = rag_pipeline._build_context(hits)
    assert sent == [hits[1]]
    assert "[来源1]" in context and "[来源2]" not in context
    assert "30" in context and "有效的小片段" in context
