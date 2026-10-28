import json
import threading
from pathlib import Path

import pytest

from app.document_schema import DocumentSource
from app.parse_jobs import ParseJobManager


def _source(tmp_path):
    path = tmp_path / "source.pdf"
    path.write_bytes(b"%PDF-1.4 fixture")
    return DocumentSource("doc", "source.pdf", "fixture", "sha", str(path))


def test_single_worker_waits_and_index_request_stops_at_review(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []

    def parse(path, **kwargs):
        calls.append(path)
        started.set()
        release.wait(timeout=5)
        return {"run_id": f"run-{len(calls)}", "counts": {"chunks": 1}}

    manager = ParseJobManager(tmp_path / "jobs", parse_runner=parse)
    try:
        first = manager.submit_parse(_source(tmp_path), index_requested=True)
        assert started.wait(timeout=5)
        second = manager.submit_parse(_source(tmp_path))
        assert manager.get_status(second["job_id"])["status"] == "queued"
        assert len(calls) == 1
        release.set()
        result = manager.wait(first["job_id"])
        manager.wait(second["job_id"])
        assert result["result"]["indexed"] is False
        assert result["result"]["next_action"] == "review"
        assert result["status"] == "completed"
    finally:
        release.set()
        manager.shutdown()


def test_cancel_running_parse_passes_event_and_stops_queued_task(tmp_path):
    started = threading.Event()

    def parse(path, cancel_event, **kwargs):
        started.set()
        assert cancel_event.wait(timeout=5)
        raise RuntimeError("cancelled fixture")

    manager = ParseJobManager(tmp_path / "jobs", parse_runner=parse)
    try:
        first = manager.submit_parse(_source(tmp_path))
        assert started.wait(timeout=5)
        queued = manager.submit_parse(_source(tmp_path))
        assert manager.cancel(queued["job_id"])["status"] == "cancelled"
        assert manager.cancel(first["job_id"])["status"] == "cancelling"
        assert manager.wait(first["job_id"])["status"] == "cancelled"
    finally:
        manager.shutdown()


def test_restart_marks_unfinished_jobs_interrupted(tmp_path):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "job-old.json").write_text(json.dumps({"job_id": "job-old", "kind": "parse",
        "status": "running", "created_at": "2026-10-26", "result": None}), encoding="utf-8")
    manager = ParseJobManager(jobs_dir)
    try:
        assert manager.get_status("job-old")["status"] == "interrupted"
        assert json.loads((jobs_dir / "job-old.json").read_text(encoding="utf-8"))["status"] == "interrupted"
    finally:
        manager.shutdown()


def test_directory_lock_prevents_multiple_local_workers(tmp_path):
    manager = ParseJobManager(tmp_path / "jobs")
    try:
        with pytest.raises(RuntimeError, match="单个uvicorn"):
            ParseJobManager(tmp_path / "jobs")
    finally:
        manager.shutdown()


def test_index_failure_is_persisted_without_false_completion(tmp_path):
    def index(run_id):
        raise ValueError("review pending")

    manager = ParseJobManager(tmp_path / "jobs", index_runner=index)
    try:
        job = manager.submit_index("run")
        result = manager.wait(job["job_id"])
        assert result["status"] == "failed" and "review pending" in result["error"]
        assert result["result"] is None
    finally:
        manager.shutdown()


def test_running_index_cannot_claim_cancellation(tmp_path):
    started, release = threading.Event(), threading.Event()

    def index(run_id):
        started.set()
        release.wait(timeout=5)
        return {"run_id": run_id, "indexed": True}

    manager = ParseJobManager(tmp_path / "jobs", index_runner=index)
    try:
        job = manager.submit_index("run")
        assert started.wait(timeout=5)
        with pytest.raises(ValueError, match="已经开始"):
            manager.cancel(job["job_id"])
        release.set()
        assert manager.wait(job["job_id"])["status"] == "completed"
    finally:
        release.set()
        manager.shutdown()
