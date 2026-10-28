import json

import pytest

from app import config
from app.index_registry import (
    activate_index, get_active_snapshot, referenced_runs,
    register_document, remove_index_references, stage_index,
)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INDEX_REGISTRY_PATH", tmp_path / "registry.sqlite3")
    monkeypatch.setattr(config, "COLLECTION_NAME", "test_chunks_v3")
    return register_document("source:1", "a.pdf")


def test_staged_index_is_hidden_and_activation_replaces_complete_snapshot(registry):
    stage_index("index1", registry, "run1", "model1", ["v1"])
    assert get_active_snapshot() == []
    activate_index("index1")
    assert get_active_snapshot()[0]["index_id"] == "index1"
    stage_index("index2", registry, "run2", "model1", ["v2", "v3"])
    assert get_active_snapshot()[0]["index_id"] == "index1"
    activate_index("index2")
    snapshot = get_active_snapshot()
    assert len(snapshot) == 1 and snapshot[0]["index_id"] == "index2"
    assert json.loads(snapshot[0]["ids_json"]) == ["v2", "v3"]
    assert referenced_runs() == {"run1", "run2"}


def test_active_snapshot_is_idempotent_and_rejects_changed_members(registry):
    stage_index("index1", registry, "run1", "model1", ["v1"])
    activate_index("index1")
    stage_index("index1", registry, "run1", "model1", ["v1"])
    activate_index("index1")
    assert len(get_active_snapshot()) == 1
    with pytest.raises(ValueError, match="成员清单"):
        stage_index("index1", registry, "run1", "model1", ["different"])


def test_embedding_identity_mismatch_does_not_replace_active(registry):
    stage_index("index1", registry, "run1", "model1", ["v1"])
    activate_index("index1")
    stage_index("index2", registry, "run2", "model2", ["v2"])
    with pytest.raises(ValueError, match="Embedding身份"):
        activate_index("index2")
    assert get_active_snapshot()[0]["index_id"] == "index1"


def test_remove_one_snapshot_retains_other_run_references(registry):
    stage_index("index1", registry, "run1", "model1", ["v1"])
    stage_index("index2", registry, "run2", "model1", ["v2"])
    activate_index("index2")
    remove_index_references(config.COLLECTION_NAME, index_id="index1")
    assert referenced_runs() == {"run2"}
    assert get_active_snapshot()[0]["index_id"] == "index2"


def test_broken_registry_is_not_treated_as_empty(tmp_path, monkeypatch):
    path = tmp_path / "broken.sqlite3"
    path.write_bytes(b"not a sqlite database")
    monkeypatch.setattr(config, "INDEX_REGISTRY_PATH", path)
    with pytest.raises(Exception):
        get_active_snapshot()
