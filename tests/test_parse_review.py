import json

import pytest

from app import config
from app.document_schema import file_hash, text_hash
from app.parse_review import approve_run, seal_run, validate_indexable_run


@pytest.fixture
def sealed_run(tmp_path, monkeypatch):
    base = tmp_path / "runs"
    root = base / "sample-run"
    root.mkdir(parents=True)
    monkeypatch.setattr(config, "PARSE_LOG_DIR", base)
    monkeypatch.setattr(config, "INDEX_REGISTRY_PATH", tmp_path / "registry.sqlite3")
    original = tmp_path / "immutable.pdf"
    original.write_bytes(b"%PDF immutable test")
    digest = file_hash(original)
    chunk = {
        "schema_version": 3, "chunk_id": "c1", "doc_id": "d1", "pdf_sha256": digest,
        "embedding_text": "风险管理", "embedding_text_sha256": text_hash("风险管理"),
        "pages": [1], "page_start": 1, "page_end": 1, "block_ids": ["b1"],
        "source_spans": [{"page": 1, "block_id": "b1", "bbox": [1, 1, 10, 10]}],
        "merge_ids": [], "parent_id": "l1", "quality_status": "pass",
    }
    files = {
        "pages.jsonl": [{"page": 1}],
        "blocks.jsonl": [{"block_id": "b1", "page": 1}],
        "logical_blocks.jsonl": [{"logical_block_id": "l1", "source_block_ids": ["b1"]}],
        "merges.jsonl": [], "chunks.jsonl": [chunk],
    }
    for name, records in files.items():
        (root / name).write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
    meta = {
        "schema_version": 3, "run_id": "sample-run", "doc_id": "d1", "source": "a.pdf",
        "pdf_path": str(original), "pdf_sha256": digest, "pipeline_fingerprint": "p1",
        "embedding": {"signature": "m1"}, "config": {}, "quality_status": "pass",
        "parse_status": "completed", "review_status": "pending",
        "counts": {"pages_total": 1, "chunks": 1},
    }
    (root / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    seal_run(root, meta)
    return root, meta


def test_pending_is_not_indexable_and_explicit_review_binds_manifest(sealed_run):
    root, _ = sealed_run
    with pytest.raises(ValueError, match="人工确认"):
        validate_indexable_run("sample-run")
    review = approve_run("sample-run", "Reviewer", notes="原页与数值已核对")
    _, records, manifest = validate_indexable_run("sample-run")
    assert len(records) == 1
    assert review["manifest_sha256"] == manifest["manifest_sha256"]
    assert review["reviewer"] == "Reviewer"


def test_changed_artifact_invalidates_human_review(sealed_run):
    root, _ = sealed_run
    approve_run("sample-run", "Reviewer")
    (root / "chunks.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="修改"):
        validate_indexable_run("sample-run")


def test_added_asset_or_missing_original_blocks_indexing(sealed_run):
    root, meta = sealed_run
    approve_run("sample-run", "Reviewer")
    (root / "unexpected.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="集合已改变"):
        validate_indexable_run("sample-run")
    (root / "unexpected.txt").unlink()
    from pathlib import Path
    Path(meta["pdf_path"]).unlink()
    with pytest.raises(ValueError, match="原件缺失"):
        validate_indexable_run("sample-run")


def test_quality_failure_cannot_be_overridden_by_approval(sealed_run):
    root, meta = sealed_run
    meta["quality_status"] = "needs_review"
    (root / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(ValueError, match="质量问题"):
        approve_run("sample-run", "Reviewer")


def test_corrupt_jsonl_is_rejected_at_sealing(sealed_run):
    root, meta = sealed_run
    (root / "chunks.jsonl").write_text('{"chunk_id":', encoding="utf-8")
    with pytest.raises(ValueError):
        seal_run(root, meta)


def test_rejected_decision_never_allows_indexing(sealed_run):
    approve_run("sample-run", "Reviewer", decision="rejected")
    with pytest.raises(ValueError, match="人工确认"):
        validate_indexable_run("sample-run")


def test_run_path_cannot_escape_parse_root(sealed_run):
    with pytest.raises(ValueError, match="run_id"):
        validate_indexable_run("../sample-run")


def test_review_cli_without_explicit_decision_only_previews(sealed_run, monkeypatch, capsys):
    import sys
    from scripts import review_parsed
    root, _ = sealed_run
    monkeypatch.setattr(review_parsed, "configure_logging", lambda: None)
    monkeypatch.setattr(sys, "argv", ["review_parsed", "--run-id", "sample-run", "--reviewer", "Reviewer"])
    assert review_parsed.main() == 0
    assert not (root / "review.json").exists()
    assert "仅预览" in capsys.readouterr().out


def test_review_cli_requires_reviewer_for_explicit_approval(sealed_run, monkeypatch, capsys):
    import sys
    from scripts import review_parsed
    root, _ = sealed_run
    monkeypatch.setattr(review_parsed, "configure_logging", lambda: None)
    monkeypatch.setattr(sys, "argv", ["review_parsed", "--run-id", "sample-run", "--approve"])
    assert review_parsed.main() == 1
    assert not (root / "review.json").exists()
    assert "reviewer" in capsys.readouterr().err
