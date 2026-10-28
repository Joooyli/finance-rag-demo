import json
from pathlib import Path

from app.parse_log import LOG_SCHEMA_VERSION, EXTRACTOR, CHUNKS_FILE, PAGES_FILE, RUN_FILE, RunLogger, list_runs, load_run, load_run_chunks
from app.rag_pipeline import build_chunk_id

def _fake_pdf(tmp_path: Path) -> Path:
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%test\n")
    return pdf

def test_run_logger_writes_one_line_per_page_and_chunk(tmp_path):
    pdf = _fake_pdf(tmp_path)
    run = RunLogger(pdf, enabled=True, base_dir=tmp_path / "logs", snapshot={})
    run.log_page({"page": 1, "mode": "native", "native_chars": 10, "chars": 10, "text": "第一页"})
    run.log_page({"page": 2, "mode": "empty", "native_chars": 0, "chars": 0, "text": ""})
    run.log_chunk({"chunk_id": "abc", "source": "sample.pdf", "page": 1,
                   "chunk_index": 0, "chars": 3, "text": "第一页"})
    run.finish(counts={"pages_total": 2, "chunks": 1})

    run_dir = Path(run.run_dir)
    pages = [json.loads(line) for line in (run_dir / PAGES_FILE).read_text(encoding="utf-8").splitlines()]
    chunks = [json.loads(line) for line in (run_dir / CHUNKS_FILE).read_text(encoding="utf-8").splitlines()]
    meta = json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))

    assert len(pages) == 2
    assert len(chunks) == 1
    assert chunks[0]["chunk_id"] == "abc"
    assert meta["status"] == "success"
    assert meta["counts"]["chunks"] == 1
    # schema 与解析器信息必须写入，便于追溯
    assert meta["schema_version"] == LOG_SCHEMA_VERSION
    assert meta["extractor"] == EXTRACTOR
    assert "extractor_version" in meta


def test_run_logger_sanitizes_lone_surrogates(tmp_path):
    """孤立代理字符会让 utf-8 编码崩溃，写入前必须替换。"""
    pdf = _fake_pdf(tmp_path)
    run = RunLogger(pdf, enabled=True, base_dir=tmp_path / "logs", snapshot={})
    run.log_page({"page": 1, "text": "文本\ud800结束", "raw_text": "\udc00"})
    run.finish(counts={"pages_total": 1})

    run_dir = Path(run.run_dir)
    pages = [json.loads(line) for line in (run_dir / PAGES_FILE).read_text(encoding="utf-8").splitlines()]
    assert pages[0]["text"].startswith("文本")
    assert "\ud800" not in pages[0]["text"]

def test_run_logger_disabled_writes_nothing(tmp_path):
    pdf = _fake_pdf(tmp_path)
    base = tmp_path / "logs"
    run = RunLogger(pdf, enabled=False, base_dir=base)
    run.log_page({"page": 1, "text": "x"})
    run.finish(counts={"pages_total": 1})

    assert not base.exists()
    assert run.run_id

def test_list_runs_and_load_chunks(tmp_path):
    pdf = _fake_pdf(tmp_path)
    base = tmp_path / "logs"
    run = RunLogger(pdf, enabled=True, base_dir=base, snapshot={})
    run.log_chunk({"chunk_id": "c1", "source": "sample.pdf", "page": 1,
                   "chunk_index": 0, "chars": 1, "text": "a"})
    run.finish(counts={"pages_total": 1, "chunks": 1})

    runs = list_runs(base_dir=base)
    assert len(runs) == 1
    assert runs[0]["run_id"] == run.run_id

    loaded = load_run(run.run_id, base_dir=base)
    assert loaded is not None
    assert loaded["source"] == "sample.pdf"

    chunks = load_run_chunks(run.run_id, base_dir=base)
    assert [chunk["chunk_id"] for chunk in chunks] == ["c1"]

def test_build_chunk_id_is_stable_and_position_aware():
    first = build_chunk_id("a.pdf", 1, 0, "文本")
    assert first == build_chunk_id("a.pdf", 1, 0, "文本")
    assert first != build_chunk_id("a.pdf", 2, 0, "文本")
    assert first != build_chunk_id("a.pdf", 1, 1, "文本")
