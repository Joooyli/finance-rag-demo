import json
import logging
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path

import pymupdf
import pytest

from app import config
from app.mineru_adapter import MinerUAdapter, MinerUError, _page_ranges


def _pdf(tmp_path, count=3):
    path = tmp_path / "input.pdf"
    with pymupdf.open() as document:
        for number in range(count):
            page = document.new_page(width=200, height=300)
            page.insert_text((20, 40), f"Page {number + 1}")
        document.save(path)
    return path


def _zip(tmp_path, pages=None, *, schema="2.0", unsafe=False):
    if pages is None:
        pages = [
            {"page_idx": 1, "blocks": [{"type": "table", "bbox": [0.1, 0.2, 0.9, 0.8],
                "index": 0, "content": [{"type": "table_body", "content":
                '<table><tr><th>金额</th><th>单位</th></tr><tr><td>-1.25</td><td>元</td></tr></table>',
                "image_path": "images/table.jpg"}]}]},
            {"page_idx": 2, "blocks": [{"type": "equation", "bbox": [0.2, 0.2, 0.7, 0.3],
                "index": 0, "content": r"V=\frac{1}{1+r}", "image_path": "images/equation.jpg"}]}]
    path = tmp_path / "fixture.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("middle_json.json", json.dumps({"schema": "docvortex.middle",
            "schema_version": schema, "pages": pages}, ensure_ascii=False))
        archive.writestr("images/table.jpg", b"table-image-fixture")
        archive.writestr("images/equation.jpg", b"equation-image-fixture")
        if unsafe:
            archive.writestr("../outside.txt", "escape")
    return path


class FixtureAdapter(MinerUAdapter):
    def __init__(self, tmp_path, fixture):
        super().__init__(python_path=tmp_path / "python.exe", model_dir=tmp_path / "models",
                         cache_dir=tmp_path / "cache", timeout_seconds=1)
        self.fixture, self.commands = fixture, []

    def preflight(self, output_dir, *, cancel_event=None):
        return {"version": "4.0.12", "runtime_fingerprint": "fixture", "tier": "standard"}

    def _run_process(self, argv, stdout_path, stderr_path, **kwargs):
        self.commands.append(argv)
        stdout_path.write_text("progress is not JSON", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        shutil.copyfile(self.fixture, Path(argv[argv.index("-o") + 1]))


def test_parse_preserves_original_pages_structure_assets_and_page_logs(tmp_path, caplog):
    adapter = FixtureAdapter(tmp_path, _zip(tmp_path))
    events = []
    with caplog.at_level(logging.INFO):
        result = adapter.parse_pages(_pdf(tmp_path), [2, 3], tmp_path / "out", event_sink=events.append)
    assert sorted(result) == [2, 3]
    table, formula = result[2][0], result[3][0]
    assert table["bbox"] == [20, 60, 180, 240]
    assert "-1.25" in table["content_text"] and "rowspan" not in table["content_text"]
    assert table["html"].startswith("<table>")
    assert (tmp_path / "out" / table["html_ref"]).is_file()
    assert all((tmp_path / "out" / ref).is_file() for ref in table["asset_refs"])
    assert formula["type"] == "formula" and r"\frac{1}{1+r}" in formula["latex"]
    assert "--pages" in adapter.commands[0] and "2-3" in adapter.commands[0]
    assert [e["page"] for e in events if e["event"] == "page_started"] == [2, 3]
    completed = [e for e in events if e["event"] == "page_completed"]
    assert [e["page"] for e in completed] == [2, 3]
    assert all(e["batch_finished"] for e in completed)
    assert "MinerU 页面=2 page_started" in caplog.text
    assert "MinerU 页面=3 page_completed" in caplog.text


def test_child_environment_forces_local_model_and_clears_inherited_remote(monkeypatch, tmp_path):
    monkeypatch.setenv("MINERU_MODEL_SOURCE", "huggingface")
    monkeypatch.setenv("MINERU_MODEL_VLM_SERVER_URL", "https://example.invalid/vlm")
    env = MinerUAdapter(model_dir=tmp_path).child_environment()
    assert env["MINERU_MODEL_SOURCE"] == "local"
    assert env["MINERU_MODEL_VLM_SERVER_URL"] == ""
    assert env["MINERU_MODEL_SMALL_BACKEND"] == "onnx"
    assert env["MINERU_MODEL_VLM_ENGINE"] == "llama-cpp"


def test_verified_cache_reuses_zip_and_tampering_forces_new_process(tmp_path):
    adapter = FixtureAdapter(tmp_path, _zip(tmp_path))
    pdf = _pdf(tmp_path)
    adapter.parse_pages(pdf, [2, 3], tmp_path / "first")
    events = []
    adapter.parse_pages(pdf, [2, 3], tmp_path / "second", event_sink=events.append)
    assert len(adapter.commands) == 1
    assert any(e["event"] == "cache_hit" for e in events)
    next((tmp_path / "cache").glob("*.zip")).write_bytes(b"damaged")
    adapter.parse_pages(pdf, [2, 3], tmp_path / "third")
    assert len(adapter.commands) == 2


@pytest.mark.parametrize("failure", ["wrong_pages", "schema", "path"])
def test_invalid_outputs_fail_every_requested_page(tmp_path, caplog, failure):
    kwargs = {"pages": [{"page_idx": 0, "blocks": []}]} if failure == "wrong_pages" else (
        {"schema": "1.0"} if failure == "schema" else {"unsafe": True})
    adapter = FixtureAdapter(tmp_path, _zip(tmp_path, **kwargs))
    events = []
    with caplog.at_level(logging.INFO), pytest.raises(MinerUError):
        adapter.parse_pages(_pdf(tmp_path), [2, 3], tmp_path / "out", event_sink=events.append)
    assert [e["page"] for e in events if e["event"] == "page_failed"] == [2, 3]
    assert not any(e["event"] == "page_completed" for e in events)
    assert not (tmp_path / "outside.txt").exists()


def test_continuation_headers_and_inline_formulas_are_preserved(tmp_path, monkeypatch):
    pages = [{"page_idx": 1, "blocks": [
        {"type": "header", "bbox": [0, 0, 1, 0.1], "content": [{"type": "text", "content": "Header"}]},
        {"type": "text", "bbox": [0.1, 0.2, 0.9, 0.3], "content": [
            {"type": "text", "content": "discount "}, {"type": "equation_inline", "content": "1/(1+r)"}]},
        {"type": "table", "bbox": [0.1, 0.4, 0.9, 0.8], "continues_prev": True,
         "content": [{"type": "table_body", "content": "<table><tr><td>value</td></tr></table>"}]}]}]
    monkeypatch.setattr(config, "HEADER_REMOVE", True)
    adapter = FixtureAdapter(tmp_path, _zip(tmp_path, pages=pages))
    blocks = adapter.parse_pages(_pdf(tmp_path), [2], tmp_path / "out")[2]
    # 适配器不提前删除页眉；所有引擎结果齐备后统一块清洗才应用HEADER_REMOVE。
    assert blocks[0]["selected"] is True
    assert "$1/(1+r)$" in blocks[1]["content_text"]
    assert blocks[2]["continues_prev"] is True


def test_cancelled_wait_emits_page_failure_and_does_not_start_process(tmp_path):
    adapter = FixtureAdapter(tmp_path, _zip(tmp_path))
    cancel = threading.Event()
    cancel.set()
    events = []
    with pytest.raises(MinerUError, match="取消"):
        adapter.parse_pages(_pdf(tmp_path), [2], tmp_path / "out", event_sink=events.append,
                            cancel_event=cancel)
    assert not adapter.commands
    assert events[-1]["event"] == "page_failed" and events[-1]["error_code"] == "cancelled"


def test_process_cancellation_terminates_process_tree(monkeypatch, tmp_path):
    class RunningProcess:
        returncode = None
        pid = 123

        def poll(self):
            return None

    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: RunningProcess())
    monkeypatch.setattr(MinerUAdapter, "_stop_process_tree", lambda self, process: calls.append(process.pid))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(MinerUError) as error:
        MinerUAdapter()._run_process(["unused"], tmp_path / "stdout", tmp_path / "stderr",
                                    cancel_event=cancel)
    assert error.value.code == "cancelled" and calls == [123]


def test_page_range_groups_adjacent_physical_pages():
    assert _page_ranges([1, 2, 3, 5, 8, 9]) == "1-3,5,8-9"


def test_list_keeps_item_boundaries_without_duplicating_inline_spans(tmp_path):
    pages = [{"page_idx": 1, "blocks": [{"type": "list", "bbox": [0.1, 0.1, 0.9, 0.9],
        "content": [{"type": "text", "content": [{"type": "text", "content": "first"}]},
                    {"type": "list", "content": [{"type": "text", "content": [
                        {"type": "text", "content": "second "},
                        {"type": "equation_inline", "content": "x+y"}]}]}]}]}]
    adapter = FixtureAdapter(tmp_path, _zip(tmp_path, pages=pages))
    text = adapter.parse_pages(_pdf(tmp_path), [2], tmp_path / "out")[2][0]["content_text"]
    assert text == "first\nsecond $x+y$"
