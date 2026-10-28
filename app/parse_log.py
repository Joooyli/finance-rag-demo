from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from app import config
from app.text_utils import sanitize_text

logger = logging.getLogger(__name__)

PAGES_FILE = "pages.jsonl"
CHUNKS_FILE = "chunks.jsonl"
RUN_FILE = "run.json"

# 日志 schema 版本：字段变动必须 +1，load_run() 会给旧记录补 1。
LOG_SCHEMA_VERSION = 3
EXTRACTOR = "hybrid:pymupdf+mineru+tesseract"


def extractor_version() -> str:
    """解析器版本，写入 run.json 便于复现；导入失败时不能影响日志。"""
    try:
        import pymupdf

        return str(getattr(pymupdf, "__version__", "unknown"))
    except Exception:  # pragma: no cover
        return "unknown"


def _sanitize_payload(value: Any) -> Any:
    """递归清洗孤立代理字符，避免 json.dumps + utf-8 抛 UnicodeEncodeError。

    本 PDF 第 17/18/23/31 页共 121 个孤立代理字符，实测会直接让写日志崩溃。
    """
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {key: _sanitize_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_payload(item) for item in value]
    return value



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
    """记录 OCR 引擎版本，便于复现；任何失败都返回 None，不影响解析。"""
    executable = config.TESSERACT_CMD or shutil.which("tesseract")
    if executable:
        try:
            result = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            lines = (result.stdout or "").strip().splitlines()
            if lines:
                return lines[0].strip()
        except Exception:
            logger.debug("无法通过命令行读取 Tesseract 版本", exc_info=True)
    try:
        import pytesseract

        if config.TESSERACT_CMD:
            pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD
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
        # 解析器与版面分析
        "layout_enabled": config.LAYOUT_ENABLED,
        "header_remove": config.HEADER_REMOVE,
        "footer_remove": config.FOOTER_REMOVE,
        "header_filter_top_ratio": config.HEADER_FILTER_TOP_RATIO,
        "header_repeat_min_pages": config.HEADER_REPEAT_MIN_PAGES,
        "header_repeat_min_ratio": config.HEADER_REPEAT_MIN_RATIO,
        "header_repeat_position_tolerance": config.HEADER_REPEAT_POSITION_TOLERANCE,
        "header_ad_patterns": list(config.HEADER_AD_PATTERNS),
        "table_strategy": config.TABLE_STRATEGY,
        # OCR
        "enable_ocr": config.ENABLE_OCR,
        "ocr_lang": config.OCR_LANG,
        "ocr_dpi": config.OCR_DPI,
        "tesseract_cmd": config.TESSERACT_CMD,
        "tesseract_version": _tesseract_version(),
        "ocr_low_text_pages": config.OCR_LOW_TEXT_PAGES,
        "min_native_text_chars": config.MIN_NATIVE_TEXT_CHARS,
        # 日志精度
        "elements_included": config.PARSE_LOG_INCLUDE_ELEMENTS,
        "element_text_max": config.PARSE_LOG_ELEMENT_TEXT_MAX,
        # 入库
        "enable_indexing": config.ENABLE_INDEXING,
        "index_batch_size": config.INDEX_BATCH_SIZE,
        "mineru_python": str(config.MINERU_PYTHON),
        "mineru_model_base_dir": str(config.MINERU_MODEL_BASE_DIR),
        "mineru_tier": config.MINERU_TIER,
        "mineru_enabled": config.MINERU_ENABLED,
        "routing_rule_version": config.ROUTING_RULE_VERSION,
        "routing_complex_area_ratio": config.ROUTING_COMPLEX_AREA_RATIO,
        "max_invalid_char_ratio": config.MAX_INVALID_CHAR_RATIO,
        "review_required": True,
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """原子写入 JSON：先写临时文件再替换，避免中途崩溃留下半个 run.json。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    safe_payload = _sanitize_payload(payload)
    tmp.write_text(json.dumps(safe_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_jsonl(path: Path, *, strict: bool = False) -> Iterator[dict[str, Any]]:
    """Yield records from a JSONL file, skipping a partially written last line."""
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                if strict:
                    raise ValueError(f"解析产物存在损坏JSON行: {path.name}")
                logger.warning("跳过无法解析的日志行: %s", path.name)


class RunLogger:
    """Writes the intermediate parse artifacts of one indexing run.

    Layout::

        data/parse_log/<run_id>/
        ├── run.json      # metadata, config snapshot and final counts
        ├── pages.jsonl   # one line per PDF page (including empty pages)
        └── chunks.jsonl  # one line per chunk

    审阅产物写入失败必须中止；只有辅助events日志允许降级。
    """

    def __init__(
        self,
        pdf_path: Path,
        *,
        enabled: bool | None = None,
        base_dir: Path | None = None,
        snapshot: dict[str, Any] | None = None,
        source_info=None,
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
        self._sealed = False

        sha = ""
        size = 0
        try:
            if self.pdf_path.exists():
                sha = file_sha256(self.pdf_path)
                size = self.pdf_path.stat().st_size
        except Exception:
            logger.warning("无法计算 PDF 校验值: %s", self.pdf_path, exc_info=True)

        display_source = source_info.source if source_info else self.pdf_path.name
        self.run_id = f"{datetime.now():%Y%m%d-%H%M%S}__{_safe_stem(display_source)}__{sha[:8] or 'unknown'}"
        self.run_dir = self.base_dir / self.run_id
        if self.enabled:
            # Avoid overwriting an existing run directory.
            suffix = 1
            while self.run_dir.exists():
                suffix += 1
                self.run_dir = self.base_dir / f"{self.run_id}-{suffix}"
            self.run_id = self.run_dir.name

        self._meta = {
            "schema_version": LOG_SCHEMA_VERSION,
            "extractor": EXTRACTOR,
            "extractor_version": extractor_version(),
            "layout_enabled": config.LAYOUT_ENABLED,
            "header": {"remove": config.HEADER_REMOVE},
            "footer": {"remove": config.FOOTER_REMOVE},
            "ocr": {
                "enabled": config.ENABLE_OCR,
                "language": config.OCR_LANG,
                "dpi": config.OCR_DPI,
                "engine": "tesseract",
            },
            "elements": {
                "included": config.PARSE_LOG_INCLUDE_ELEMENTS,
                "text_max": config.PARSE_LOG_ELEMENT_TEXT_MAX,
            },
            "run_id": self.run_id,
            "source": source_info.source if source_info else self.pdf_path.name,
            "doc_id": source_info.doc_id if source_info else None,
            "pdf_path": str(self.pdf_path),
            "pdf_sha256": sha or None,
            "pdf_bytes": size or None,
            "started_at": self.started_at,
            "finished_at": None,
            "status": "running",
            "parse_status": "running", "quality_status": "needs_review",
            "review_status": "pending", "index_status": "not_indexed",
            "error": None,
            "config": config_snapshot() if snapshot is None else snapshot,
            "counts": {},
            "index": {"enabled": None, "collection": None, "chunks": None, "collection_total": None},
        }
        if self.enabled:
            self._start()

    # ------------------------------------------------------------------ setup
    def _start(self) -> None:
        # 页面/chunk是人工审核和入库依据，创建失败不能伪装为解析成功。
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._pages_handle = (self.run_dir / PAGES_FILE).open("a", encoding="utf-8")
        self._chunks_handle = (self.run_dir / CHUNKS_FILE).open("a", encoding="utf-8")
        _write_json_atomic(self.run_dir / RUN_FILE, self._meta)
        logger.info("解析日志目录: %s", self.run_dir)

    # ----------------------------------------------------------------- writing
    def _append(self, handle, record: dict[str, Any]) -> None:
        if not self.enabled or handle is None:
            return
        if self._sealed:
            raise ValueError("密封后的解析产物不可继续写入")
        safe_record = _sanitize_payload(record)
        handle.write(json.dumps(safe_record, ensure_ascii=False) + "\n")
        handle.flush()

    def log_page(self, record: dict[str, Any]) -> None:
        self._append(self._pages_handle, record)

    def log_chunk(self, record: dict[str, Any]) -> None:
        self._append(self._chunks_handle, record)

    def write_records(self, name: str, records: list[dict]) -> None:
        if not self.enabled:
            return
        if self._sealed or Path(name).name != name:
            raise ValueError("无效产物名称或产物已密封")
        with (self.run_dir / name).open("w", encoding="utf-8") as handle:
            for record in records:
                self._append(handle, record)

    def log_event(self, event: dict) -> None:
        # 引擎自身负责控制台逐页打印；此处仅保存带run身份的事件证据。
        if self.enabled:
            try:
                payload = {"schema_version": LOG_SCHEMA_VERSION, "ts": now_iso(),
                           "run_id": self.run_id, "doc_id": self._meta.get("doc_id"), **event}
                with (self.run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(_sanitize_payload(payload), ensure_ascii=False) + "\n")
            except OSError:
                logger.warning("辅助事件写入失败 run_id=%s", self.run_id, exc_info=True)

    # ---------------------------------------------------------------- finishing
    def finish(
        self,
        *,
        counts: dict[str, Any] | None = None,
        index: dict[str, Any] | None = None,
        status: str = "success",
        quality_status: str | None = None,
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
                "parse_status": "completed" if status == "success" else status,
            }
        )
        if quality_status:
            self._meta["quality_status"] = quality_status
        if index is not None:
            self._meta["index"] = {**self._meta.get("index", {}), **index}
        if self.enabled:
            _write_json_atomic(self.run_dir / RUN_FILE, self._meta)
            logger.info(
                "解析完成 run_id=%s 模式=%s 切块=%s 状态=%s",
                self.run_id,
                self.counts.get("pages_by_mode", {}),
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
        # 旧日志没有 schema_version，补 1 以便读取侧做兼容分支。
        data.setdefault("schema_version", 1)
        runs.append(data)
        if limit is not None and len(runs) >= limit:
            break
    return runs


def load_run(run_id: str, base_dir: Path | None = None) -> dict[str, Any] | None:
    run_dir = resolve_run_dir(run_id, base_dir)
    run_file = run_dir / RUN_FILE
    if not run_file.exists():
        return None
    try:
        data = json.loads(run_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    data.setdefault("run_id", run_id)
    data.setdefault("schema_version", 1)
    data["run_dir"] = str(run_dir)
    return data


def load_run_chunks(run_id: str, base_dir: Path | None = None) -> list[dict[str, Any]]:
    path = resolve_run_dir(run_id, base_dir) / CHUNKS_FILE
    if not path.exists():
        return []
    return list(read_jsonl(path))


def load_run_pages(run_id: str, base_dir: Path | None = None) -> list[dict[str, Any]]:
    path = resolve_run_dir(run_id, base_dir) / PAGES_FILE
    if not path.exists():
        return []
    return list(read_jsonl(path))


def latest_run_id(base_dir: Path | None = None) -> str | None:
    runs = list_runs(limit=1, base_dir=base_dir)
    return runs[0]["run_id"] if runs else None


def resolve_run_dir(run_id: str, base_dir: Path | None = None) -> Path:
    root = Path(base_dir if base_dir is not None else config.PARSE_LOG_DIR).resolve()
    target = (root / run_id).resolve()
    if not run_id or target.parent != root:
        raise ValueError("无效run_id")
    return target
