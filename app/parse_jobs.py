"""本机单 worker 队列：请求立即返回，解析/入库顺序执行并持久化任务状态。"""

from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app import config
from app.document_schema import DocumentSource

logger = logging.getLogger(__name__)
_RUNNING = {"queued", "running", "cancelling"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class ParseJobManager:
    def __init__(self, jobs_dir=None, *, parse_runner=None, index_runner=None):
        self.jobs_dir = Path(jobs_dir or config.PARSE_JOBS_DIR)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._directory_lock = (self.jobs_dir / "worker.lock").open("a+b")
        self._claim_directory()
        self._jobs: dict[str, dict] = {}
        self._events: dict[str, threading.Event] = {}
        self._futures = {}
        self._parse_runner, self._index_runner = parse_runner, index_runner
        self._closed = False
        # 重启后不自动重跑可能已写产物的任务，避免重复解析/入库或伪造完成状态。
        for path in self.jobs_dir.glob("job-*.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if path.stem != job.get("job_id"):
                    raise ValueError("job identity does not match filename")
                if job.get("status") in _RUNNING:
                    job.update(status="interrupted", finished_at=_now(),
                               error="服务重启，原任务已中断；请检查产物后重新提交")
                    self._save(job)
                self._jobs[job["job_id"]] = job
            except (OSError, ValueError, KeyError):
                logger.exception("无法恢复任务记录: %s", path.name)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf-worker")

    def _claim_directory(self):
        # 限制同一任务目录只有一个服务进程；避免多个uvicorn worker各自运行本地模型。
        try:
            self._directory_lock.seek(0)
            if os.name == "nt":
                import msvcrt
                if self._directory_lock.read(1) == b"":
                    self._directory_lock.write(b"0")
                    self._directory_lock.flush()
                self._directory_lock.seek(0)
                msvcrt.locking(self._directory_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._directory_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._directory_lock.close()
            raise RuntimeError("本地解析队列已被其他服务进程使用，请使用单个uvicorn worker") from exc

    def _save(self, job):
        path = self.jobs_dir / f"{job['job_id']}.json"
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def _submit(self, kind, **payload):
        with self._lock:
            if self._closed:
                raise RuntimeError("解析队列已经关闭")
            job_id = f"job-{uuid4().hex}"
            job = {"job_id": job_id, "kind": kind, "status": "queued", "created_at": _now(),
                   "started_at": None, "finished_at": None, "result": None, "error": None, **payload}
            self._events[job_id] = threading.Event()
            self._jobs[job_id] = job
            self._save(job)
            self._futures[job_id] = self._executor.submit(self._run, job_id)
            return dict(job)

    def submit_parse(self, source_info: DocumentSource, *, index_requested=False):
        return self._submit("parse", source_info=source_info.to_dict(), index_requested=bool(index_requested))

    def submit_index(self, run_id: str):
        return self._submit("index", run_id=run_id)

    def _run(self, job_id):
        with self._lock:
            job = self._jobs[job_id]
            if job["status"] != "queued":
                return
            job.update(status="running", started_at=_now())
            self._save(job)
        event = self._events[job_id]
        try:
            if job["kind"] == "parse":
                source = DocumentSource(**job["source_info"])
                if self._parse_runner is None:
                    from app.rag_pipeline import parse_document, parse_result_payload
                    parsed = parse_document(Path(source.pdf_path), write_log=True, source_info=source,
                                            cancel_event=event)
                    result = parse_result_payload(parsed)
                else:
                    result = self._parse_runner(Path(source.pdf_path), write_log=True,
                                                source_info=source, cancel_event=event)
                # 新解析尚无人工确认；index=true仅保留请求意图，不能在这里直接入库。
                result = {**result, "indexed": False, "next_action": "review"}
            else:
                if self._index_runner is None:
                    from app.rag_pipeline import index_parsed_run
                    result = index_parsed_run(job["run_id"])
                else:
                    result = self._index_runner(job["run_id"])
            with self._lock:
                job.update(status="cancelled" if event.is_set() else "completed", result=result,
                           finished_at=_now(), run_id=result.get("run_id", job.get("run_id")))
                self._save(job)
        except BaseException as exc:
            with self._lock:
                job.update(status="cancelled" if event.is_set() else "failed", error=f"{type(exc).__name__}: {exc}",
                           finished_at=_now())
                self._save(job)
            logger.exception("后台任务失败 job_id=%s kind=%s", job_id, job["kind"])

    def get_status(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            return json.loads(json.dumps(job)) if job else None

    def list_jobs(self, limit=20):
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda job: job["created_at"], reverse=True)
            return json.loads(json.dumps(jobs[:limit]))

    def cancel(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job["status"] not in _RUNNING:
                return dict(job)
            if job["kind"] == "index" and job["status"] != "queued":
                # 激活中的索引任务不能在没有协作取消协议时声称已安全取消。
                raise ValueError("入库任务已经开始，当前仅允许取消排队中的入库任务")
            self._events[job_id].set()
            if job["status"] == "queued":
                self._futures[job_id].cancel()
                job.update(status="cancelled", finished_at=_now())
            else:
                job["status"] = "cancelling"
            self._save(job)
            return dict(job)

    def wait(self, job_id, timeout=10):
        self._futures[job_id].result(timeout=timeout)
        return self.get_status(job_id)

    def shutdown(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for job_id, job in self._jobs.items():
                if job["status"] == "queued" or (job["status"] == "running" and job["kind"] == "parse"):
                    self.cancel(job_id)
        # 先完成/终止任务，再释放目录锁；服务退出时不遗留正在使用模型的worker。
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._directory_lock.close()


_manager: ParseJobManager | None = None
_manager_lock = threading.Lock()


def get_job_manager() -> ParseJobManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = ParseJobManager()
        return _manager


def shutdown_job_manager():
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.shutdown()
            _manager = None
