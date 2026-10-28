"""真实PDF/临时Chroma的应用链路验证；不加载模型、不访问正式数据库。"""
from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest

from app import config, embeddings, parse_log, rag_pipeline, vector_store
from app.document_schema import make_block
from app.index_registry import get_active_snapshot, stage_index
from app.parse_review import approve_run, seal_run, validate_indexable_run


def write_pdf(path: Path, text: str) -> None:
    document = pymupdf.open()
    page = document.new_page(width=600, height=800)
    page.insert_text((50, 100), text, fontsize=12)
    document.save(path)
    document.close()


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    for setting, value in {
        "PARSE_LOG_DIR": tmp_path / "runs", "DOCUMENT_DIR": tmp_path / "documents",
        "INDEX_REGISTRY_PATH": tmp_path / "registry.sqlite3", "CHROMA_DIR": tmp_path / "chroma",
        "COLLECTION_NAME": "test_pipeline_chunks", "PARSE_LOG_ENABLED": True,
        "LOG_TO_FILE": False, "CHUNK_SIZE": 150, "CHUNK_OVERLAP": 10,
    }.items():
        monkeypatch.setattr(config, setting, value)
    signature = {"signature": "fixture-model-v1", "dimension": 3, "token_budget": 64, "max_seq_length": 64}
    monkeypatch.setattr(embeddings, "get_embedding_signature", lambda: dict(signature))
    monkeypatch.setattr(embeddings, "get_token_budget", lambda: 64)
    monkeypatch.setattr(embeddings, "count_tokens", lambda text: len(text) + 2)
    monkeypatch.setattr(embeddings, "embed_texts", lambda texts: [[1.0, 0.0, 0.0] for _ in texts])
    monkeypatch.setattr(embeddings, "embed_query", lambda text: [1.0, 0.0, 0.0])
    monkeypatch.setattr(parse_log, "_tesseract_version", lambda: "fixture-tesseract")

    def native_extract(pdf_path, page_sink=None, **kwargs):
        # loader路由自身已有独立测试；这里从真实PDF读取文本，验证其后应用链路。
        pages = []
        with pymupdf.open(pdf_path) as document:
            for index, page in enumerate(document):
                text = page.get_text("text").strip()
                block = make_block(index + 1, "paragraph", text, [50, 80, 550, 110], "pymupdf")
                record = {"page": index + 1, "source": Path(pdf_path).name,
                          "geometry": {"width": 600, "height": 800}, "blocks": [block], "text": text,
                          "mode": "native", "route": "native", "status": "completed",
                          "quality": {"status": "pass", "issues": []}, "ocr_used": False}
                pages.append(record)
                if page_sink:
                    page_sink(record)
        return pages

    monkeypatch.setattr(rag_pipeline, "extract_pdf_pages", native_extract)
    return tmp_path


def test_parse_review_index_reindex_and_new_version_activation(pipeline):
    pdf = pipeline / "report.pdf"
    write_pdf(pdf, "Market risk depends on volatility.")
    parsed = rag_pipeline.parse_document(pdf)
    assert parsed.quality_status == "pass"
    assert (Path(parsed.run_dir) / "manifest.json").is_file()
    with pytest.raises(ValueError, match="人工确认"):
        rag_pipeline.index_parsed_run(parsed.run_id)
    assert vector_store.collection_count() == 0

    approve_run(parsed.run_id, "Fixture reviewer")
    first = rag_pipeline.index_parsed_run(parsed.run_id)
    hits = rag_pipeline.retrieve("Market risk", top_k=3)
    assert len(hits) == 1
    hit = hits[0]
    assert hit["text"] == parsed.chunks[0].record["embedding_text"]
    assert hit["chunk_id"] == parsed.chunks[0].id
    assert hit["vector_record_id"] != hit["chunk_id"]
    assert hit["pages"] == [1] and hit["page_start"] == hit["page_end"] == 1
    assert hit["review_status"] == "approved" and hit["quality_status"] == "pass"
    assert vector_store.get_active_chunk(hit["vector_record_id"])["chunk_id"] == hit["chunk_id"]
    assert vector_store.get_active_chunk(hit["chunk_id"]) is None
    raw_count = vector_store.get_collection().count()

    again = rag_pipeline.index_parsed_run(parsed.run_id)
    assert again["index_id"] == first["index_id"]
    assert vector_store.get_collection().count() == raw_count

    # 新版本保持同一doc_id，激活前仍读取旧版本，激活后旧向量仅作历史保留。
    write_pdf(pdf, "Liquidity risk depends on cash flows.")
    newer = rag_pipeline.parse_document(pdf)
    assert newer.chunks[0].record["doc_id"] == parsed.chunks[0].record["doc_id"]
    assert newer.chunks[0].record["pdf_sha256"] != parsed.chunks[0].record["pdf_sha256"]
    assert rag_pipeline.retrieve("risk")[0]["index_id"] == first["index_id"]
    approve_run(newer.run_id, "Fixture reviewer")
    second = rag_pipeline.index_parsed_run(newer.run_id)
    assert second["index_id"] != first["index_id"]
    assert vector_store.collection_count() == 1
    assert vector_store.get_collection().count() == raw_count + 1
    assert vector_store.get_active_chunk(hit["vector_record_id"]) is None
    assert "Liquidity" in rag_pipeline.retrieve("risk")[0]["text"]
    assert get_active_snapshot()[0]["index_id"] == second["index_id"]


def test_staged_vectors_are_hidden_from_all_public_reads(pipeline):
    from app.document_schema import text_hash
    doc = "fixture-staged-doc"
    stage_index("fixture-staged", doc, "staged-run", "fixture-model-v1", ["staged-vector"])
    metadata = {"index_id": "fixture-staged", "chunk_id": "logical-staged", "pages_json": "[1]",
                "page_start": 1, "source": "staged.pdf", "embedding_text_sha256": text_hash("Hidden")}
    vector_store.upsert_chunks(["staged-vector"], ["Hidden"], [metadata], [[1.0, 0.0, 0.0]],
                               embedding_signature="fixture-model-v1")
    assert vector_store.get_collection().count() == 1
    assert vector_store.collection_count() == 0
    assert vector_store.list_active_chunks() == []
    assert vector_store.get_active_chunk("staged-vector") is None
    assert vector_store.search_chunks([1.0, 0.0, 0.0], 5) == []
    assert rag_pipeline.retrieve("Hidden") == []


def test_index_true_does_not_bypass_review(pipeline):
    pdf = pipeline / "report.pdf"
    write_pdf(pdf, "Market risk.")
    result = rag_pipeline.index_pdf(pdf, index=True)
    assert result["index_requested"] is True
    assert result["indexed"] is False and result["review_status"] == "pending"
    assert vector_store.collection_count() == 0


def test_manifest_tampering_and_source_reference_corruption_are_rejected(pipeline):
    pdf = pipeline / "report.pdf"
    write_pdf(pdf, "Market risk.")
    parsed = rag_pipeline.parse_document(pdf)
    root = Path(parsed.run_dir)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source"] = "tampered.pdf"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest校验失败"):
        validate_indexable_run(parsed.run_id, require_review=False)
    records = [json.loads(line) for line in (root / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    records[0]["source_spans"][0]["block_id"] = "nonexistent"
    (root / "chunks.jsonl").write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
    meta = json.loads((root / "run.json").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="不存在"):
        seal_run(root, meta)


def test_oversize_atomic_formula_remains_review_only(pipeline, monkeypatch):
    pdf = pipeline / "report.pdf"
    write_pdf(pdf, "Formula fixture.")
    expression = "$$" + "x_1+" * 30 + "x_2$$"

    def formula_extract(path, page_sink=None, **kwargs):
        block = make_block(1, "formula", expression, [50, 80, 550, 110], "mineru", latex=expression)
        page = {"page": 1, "blocks": [block], "text": expression, "mode": "mineru",
                "geometry": {"width": 600, "height": 800}, "quality": {"status": "pass"}}
        page_sink(page)
        return [page]

    monkeypatch.setattr(rag_pipeline, "extract_pdf_pages", formula_extract)
    parsed = rag_pipeline.parse_document(pdf)
    assert parsed.quality_status == "needs_review"
    assert parsed.chunks[0].record["embedding_tokens"] > 64
    assert parsed.chunks[0].record["text"] == expression
    with pytest.raises(ValueError, match="质量问题"):
        approve_run(parsed.run_id, "Fixture reviewer")


def test_source_span_page_must_match_referenced_physical_block(pipeline):
    pdf = pipeline / "report.pdf"
    document = pymupdf.open()
    document.new_page(width=600, height=800).insert_text((50, 100), "First page risk.")
    document.new_page(width=600, height=800).insert_text((50, 100), "Second page risk.")
    document.save(pdf)
    document.close()
    parsed = rag_pipeline.parse_document(pdf)
    root = Path(parsed.run_dir)
    records = [json.loads(line) for line in (root / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    # ID本身确实存在，但物理页不相符，不能重新密封成合法来源。
    records[0]["source_spans"][0]["block_id"] = records[1]["block_ids"][0]
    (root / "chunks.jsonl").write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
    meta = json.loads((root / "run.json").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="来源|页面|页号"):
        seal_run(root, meta)
