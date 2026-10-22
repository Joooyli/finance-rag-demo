from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from app import config

logger = logging.getLogger(__name__)

PAGES_FILE = "pages.jsonl"
CHUNKS_FILE = "chunks.jsonl"
RUN_FILE = "run.json"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_stem(name: str) -> str:
    stem = Path(name).stem
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", stem).strip("-")
    return cleaned[:40] or "document"


def _tesseract_version() -> str | None:
    if not config.TESSERACT_CMD:
        return None
    try:
        import pytesseract

        return str(pytesseract.get_tesseract_version())
    except Exception:
        return None


def config_snapshot() -> dict[str, Any]:
    """Record everything needed to reproduce this parse run."""
    return {
        "chunk_size": config.CHUNK_SIZE,
        "chunk_overlap": config.CHUNK_OVERLAP,
        "embedding_model": config.EMBEDDING_MODEL,
        "collection": config.COLLECTION_NAME,
        "enable_ocr": config.ENABLE_OCR,
        "ocr_lang": config.OCR_LANG,
        "tesseract_cmd": config.TESSERACT_CMD,
        "tesseract_version": _tesseract_version(),
        "ocr_low_text_pages": config.OCR_LOW_TEXT_PAGES,
        "min_native_text_chars": config.MIN_NATIVE_TEXT_CHARS,
        "enable_indexing": config.ENABLE_INDEXING,
        "index_batch_size": config.INDEX_BATCH_SIZE,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield records from a JSONL file, skipping a partially written last line."""
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.warning("跳过无法解析的日志行: %s", path.name)


class RunLogger:
    """Writes the intermediate parse artifacts of one indexing run.

    Layout::

        data/parse_log/<run_id>/
        ├── run.json      # metadata, config snapshot and final counts
        ├── pages.jsonl   # one line per PDF page (including empty pages)
        └── chunks.jsonl  # one line per chunk

    All disk writes are best-effort: a logging failure is reported as a warning
    and never aborts parsing or indexing.
    """

    def __init__(
        self,
        pdf_path: Path,
        *,
        enabled: bool | None = None,
        base_dir: Path | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        self.pdf_path = Path(pdf_path)
        self.enabled = config.PARSE_LOG_ENABLED if enabled is None else bool(enabled)
        self.base_dir = Path(base_dir) if base_dir is not None else config.PARSE_LOG_DIR
        self.started_at = now_iso()
        self.finished_at: str | None = None
        self.status = "running"
        self.error: str | None = None
        self.counts: dict[str, Any] = {}
        self._meta: dict[str, Any] = {}
        self._pages_handle = None
        self._chunks_handle = None

        sha = ""
        size = 0
        try:
            if self.pdf_path.exists():
                sha = file_sha256(self.pdf_path)
                size = self.pdf_path.stat().st_size
        except Exception:
            logger.warning("无法计算 PDF 校验值: %s", self.pdf_path, exc_info=True)

        self.run_id = f"{datetime.now():%Y%m%d-%H%M%S}__{_safe_stem(self.pdf_path.name)}__{sha[:8] or 'unknown'}"
        self.run_dir = self.base_dir / self.run_id
        if self.enabled:
            # Avoid overwriting an existing run directory.
            suffix = 1
            while self.run_dir.exists():
                suffix += 1
                self.run_dir = self.base_dir / f"{self.run_id}-{suffix}"
            self.run_id = self.run_dir.name

        self._meta = {
            "run_id": self.run_id,
            "source": self.pdf_path.name,
            "pdf_path": str(self.pdf_path),
            "pdf_sha256": sha or None,
            "pdf_bytes": size or None,
            "started_at": self.started_at,
            "finished_at": None,
            "status": "running",
            "error": None,
            "config": snapshot or config_snapshot(),
            "counts": {},
            "index": {"enabled": None, "collection": None, "chunks": None, "collection_total": None},
        }
        if self.enabled:
            self._start()

    # ------------------------------------------------------------------ setup
    def _start(self) -> None:
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self._pages_handle = (self.run_dir / PAGES_FILE).open("a", encoding="utf-8")
            self._chunks_handle = (self.run_dir / CHUNKS_FILE).open("a", encoding="utf-8")
            _write_json_atomic(self.run_dir / RUN_FILE, self._meta)
            logger.info("解析日志目录: %s", self.run_dir)
        except Exception:
            logger.warning("无法创建解析日志目录，本次仅记录到运行日志", exc_info=True)
            self.enabled = False

    # ----------------------------------------------------------------- writing
    def _append(self, handle, record: dict[str, Any]) -> None:
        if not self.enabled or handle is None:
            return
        try:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
        except Exception:
            logger.warning("写入解析日志失败: %s", record.get("page", record.get("chunk_id")), exc_info=True)

    def log_page(self, record: dict[str, Any]) -> None:
        self._append(self._pages_handle, record)

    def log_chunk(self, record: dict[str, Any]) -> None:
        self._append(self._chunks_handle, record)

    # ---------------------------------------------------------------- finishing
    def finish(
        self,
        *,
        counts: dict[str, Any] | None = None,
        index: dict[str, Any] | None = None,
        status: str = "success",
    ) -> None:
        self.status = status
        self.finished_at = now_iso()
        if counts is not None:
            self.counts = counts
        self._meta.update(
            {
                "status": self.status,
                "finished_at": self.finished_at,
                "error": self.error,
                "counts": self.counts,
            }
        )
        if index is not None:
            self._meta["index"] = {**self._meta.get("index", {}), **index}
        if self.enabled:
            try:
                _write_json_atomic(self.run_dir / RUN_FILE, self._meta)
            except Exception:
                logger.warning("写入 run.json 失败: %s", self.run_dir, exc_info=True)
            logger.info(
                "解析完成 run_id=%s 页数=%s 切块=%s 状态=%s",
                self.run_id,
                self.counts.get("pages_total"),
                self.counts.get("chunks"),
                self.status,
            )
        self.close()

    def fail(self, exc: BaseException) -> None:
        self.error = f"{type(exc).__name__}: {exc}"
        self.finish(status="failed")

    def close(self) -> None:
        for attribute in ("_pages_handle", "_chunks_handle"):
            handle = getattr(self, attribute)
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
                setattr(self, attribute, None)

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.fail(exc)
            return False
        self.finish()
        return False


# ---------------------------------------------------------------- run queries
def list_runs(limit: int | None = None, base_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return parse runs, newest first. Missing run.json files are skipped."""
    root = Path(base_dir) if base_dir is not None else config.PARSE_LOG_DIR
    if not root.exists():
        return []
    runs: list[dict[str, Any]] = []
    for directory in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
        if not directory.is_dir():
            continue
        run_file = directory / RUN_FILE
        if not run_file.exists():
            continue
        try:
            data = json.loads(run_file.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("无法读取 %s", run_file, exc_info=True)
            continue
        data.setdefault("run_id", directory.name)
        runs.append(data)
        if limit is not None and len(runs) >= limit:
            break
    return runs


def load_run(run_id: str, base_dir: Path | None = None) -> dict[str, Any] | None:
    root = Path(base_dir) if base_dir is not None else config.PARSE_LOG_DIR
    run_dir = root / run_id
    run_file = run_dir / RUN_FILE
    if not run_file.exists():
        return None
    try:
        data = json.loads(run_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    data.setdefault("run_id", run_id)
    data["run_dir"] = str(run_dir)
    return data


def load_run_chunks(run_id: str, base_dir: Path | None = None) -> list[dict[str, Any]]:
    root = Path(base_dir) if base_dir is not None else config.PARSE_LOG_DIR
    path = root / run_id / CHUNKS_FILE
    if not path.exists():
        return []
    return list(read_jsonl(path))


def load_run_pages(run_id: str, base_dir: Path | None = None) -> list[dict[str, Any]]:
    root = Path(base_dir) if base_dir is not None else config.PARSE_LOG_DIR
    path = root / run_id / PAGES_FILE
    if not path.exists():
        return []
    return list(read_jsonl(path))


def latest_run_id(base_dir: Path | None = None) -> str | None:
    runs = list_runs(limit=1, base_dir=base_dir)
    return runs[0]["run_id"] if runs else None
