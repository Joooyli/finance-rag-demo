from pathlib import Path

import pytest

from app import config
from app.document_source import prepare_source


@pytest.fixture
def source_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INDEX_REGISTRY_PATH", tmp_path / "registry.sqlite3")
    monkeypatch.setattr(config, "DOCUMENT_DIR", tmp_path / "documents")
    return tmp_path


def test_input_update_retains_old_immutable_pdf(source_dirs):
    original = source_dirs / "report.pdf"
    original.write_bytes(b"%PDF original")
    first = prepare_source(original)
    original.write_bytes(b"%PDF changed")
    second = prepare_source(original)
    assert first.doc_id == second.doc_id
    assert first.pdf_sha256 != second.pdf_sha256
    assert Path(first.pdf_path).read_bytes() == b"%PDF original"
    assert Path(second.pdf_path).read_bytes() == b"%PDF changed"


def test_upload_identity_is_explicit_and_same_name_does_not_override(source_dirs):
    original = source_dirs / "report.pdf"
    original.write_bytes(b"%PDF original")
    first = prepare_source(original, source_key="upload:1")
    second = prepare_source(original, source_key="upload:2")
    assert first.doc_id != second.doc_id
    updated = prepare_source(original, doc_id=first.doc_id, source_key="upload:3")
    assert updated.doc_id == first.doc_id
    with pytest.raises(ValueError, match="doc_id"):
        prepare_source(original, doc_id="not-registered")


def test_saved_source_corruption_is_detected(source_dirs):
    original = source_dirs / "report.pdf"
    original.write_bytes(b"%PDF original")
    first = prepare_source(original)
    Path(first.pdf_path).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="校验失败"):
        prepare_source(original)
