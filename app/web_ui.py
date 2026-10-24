from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import config
from app.parse_log import list_runs, load_run, load_run_chunks, load_run_pages
from app.vector_store import get_collection

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/ui", tags=["ui"], include_in_schema=False)

MAX_PAGE_SIZE = 200


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _where(source: str | None, page: int | None) -> dict | None:
    clauses: list[dict] = []
    if source:
        clauses.append({"source": source})
    if page is not None:
        clauses.append({"page": page})
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    # Chroma requires a single operator when several conditions are combined.
    return {"$and": clauses}


def _query_chunks(
    *,
    q: str | None,
    source: str | None,
    page: int | None,
    limit: int,
    offset: int,
) -> tuple[list[dict], int, int]:
    collection = get_collection()
    where = _where(source, page)

    total_in_collection = collection.count()
    filters: dict = {}
    if where:
        filters["where"] = where
    if q:
        filters["where_document"] = {"$contains": q}

    filtered_total = len(collection.get(include=["metadatas"], **filters)["ids"])

    rows: list[dict] = []
    records = collection.get(limit=limit, offset=offset, include=["documents", "metadatas"], **filters)
    for chunk_id, text, metadata in zip(
        records["ids"], records["documents"], records["metadatas"]
    ):
        metadata = metadata or {}
        rows.append(
            {
                "chunk_id": chunk_id,
                "text": text or "",
                "chars": len(text or ""),
                "source": metadata.get("source", ""),
                "page": metadata.get("page", ""),
                "chunk_index": metadata.get("chunk_index", ""),
                "run_id": metadata.get("run_id", ""),
            }
        )
    return rows, filtered_total, total_in_collection


def _sources() -> list[tuple[str, int]]:
    collection = get_collection()
    if collection.count() == 0:
        return []
    metadatas = collection.get(include=["metadatas"])["metadatas"]
    counter = Counter((meta or {}).get("source", "unknown") for meta in metadatas)
    return sorted(counter.items())


@router.get("", response_class=HTMLResponse)
def ui_home(request: Request):
    collection = get_collection()
    runs = list_runs(limit=10)
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "collection_name": collection.name,
            "chunk_total": collection.count(),
            "sources": _sources(),
            "runs": runs,
            "chroma_dir": str(config.CHROMA_DIR),
            "parse_log_dir": str(config.PARSE_LOG_DIR),
            "indexing_enabled": config.ENABLE_INDEXING,
            "parse_log_enabled": config.PARSE_LOG_ENABLED,
        },
    )


@router.get("/chunks", response_class=HTMLResponse)
def ui_chunks(
    request: Request,
    q: str | None = None,
    source: str | None = None,
    page: str | None = None,
    limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
):
    page_number = None
    if page not in (None, ""):
        try:
            page_number = int(page)
        except ValueError:
            page_number = None

    try:
        rows, filtered_total, total_in_collection = _query_chunks(
            q=q, source=source, page=page_number, limit=limit, offset=offset
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the page
        logger.exception("读取切块失败")
        raise HTTPException(status_code=500, detail=f"读取切块失败：{exc}") from exc

    next_offset = offset + limit if offset + limit < filtered_total else None
    prev_offset = max(0, offset - limit) if offset > 0 else None
    return templates.TemplateResponse(
        request,
        "chunks.html",
        {
            "rows": rows,
            "filtered_total": filtered_total,
            "total_in_collection": total_in_collection,
            "q": q or "",
            "source": source or "",
            "page": page or "",
            "limit": limit,
            "offset": offset,
            "next_offset": next_offset,
            "prev_offset": prev_offset,
            "sources": _sources(),
        },
    )


@router.get("/chunks/{chunk_id}", response_class=HTMLResponse)
def ui_chunk_detail(request: Request, chunk_id: str):
    collection = get_collection()
    record = collection.get(ids=[chunk_id], include=["documents", "metadatas"])
    if not record["ids"]:
        raise HTTPException(status_code=404, detail="切块不存在")

    metadata = record["metadatas"][0] or {}
    text = record["documents"][0] or ""
    siblings = []
    if metadata.get("source") and metadata.get("page"):
        sibling_records = collection.get(
            where=_where(metadata["source"], int(metadata["page"])),
            include=["documents", "metadatas"],
        )
        for sibling_id, sibling_text, sibling_meta in zip(
            sibling_records["ids"], sibling_records["documents"], sibling_records["metadatas"]
        ):
            sibling_meta = sibling_meta or {}
            siblings.append(
                {
                    "chunk_id": sibling_id,
                    "chunk_index": sibling_meta.get("chunk_index", ""),
                    "chars": len(sibling_text or ""),
                    "is_current": sibling_id == chunk_id,
                }
            )
        siblings.sort(key=lambda item: str(item["chunk_index"]).zfill(6))

    return templates.TemplateResponse(
        request,
        "chunk_detail.html",
        {
            "chunk_id": chunk_id,
            "text": text,
            "metadata": metadata,
            "chars": len(text),
            "siblings": siblings,
        },
    )


@router.get("/runs", response_class=HTMLResponse)
def ui_runs(request: Request):
    return templates.TemplateResponse(
        request,
        "runs.html",
        {"runs": list_runs(limit=100), "parse_log_dir": str(config.PARSE_LOG_DIR)},
    )


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def ui_run_detail(request: Request, run_id: str):
    run = load_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"解析记录不存在：{run_id}")

    pages = load_run_pages(run_id)
    chunks = load_run_chunks(run_id)

    chunks_per_page: Counter = Counter()
    for chunk in chunks:
        chunks_per_page[chunk.get("page")] += 1

    total_pdf_pages = (run.get("counts") or {}).get("pages_total") or len(pages)
    page_rows = []
    for page in pages:
        number = page.get("page")
        # schema v2：mode / elements / raw_text / text；schema v1：native_chars 等旧字段。
        if "mode" in page:
            mode = page.get("mode") or ""
            elements_count = len(page.get("elements") or [])
            raw_chars = page.get("raw_chars") or len(page.get("raw_text") or "")
            ocr_used = bool(page.get("ocr_used"))
        else:
            mode = "legacy"
            elements_count = 0
            raw_chars = page.get("native_chars", 0) + page.get("image_ocr_chars", 0) + page.get("full_page_ocr_chars", 0)
            ocr_used = bool(page.get("image_ocr_chars") or page.get("full_page_ocr_chars"))

        page_rows.append(
            {
                "page": number,
                "mode": mode,
                "ocr_used": ocr_used,
                "elements_count": elements_count,
                "raw_chars": raw_chars,
                "chars": page.get("chars", len(page.get("text") or "")),
                "chunks": chunks_per_page.get(number, 0),
                "text": page.get("text", ""),
            }
        )
    page_rows.sort(key=lambda item: item["page"] if isinstance(item["page"], int) else 0)

    missing_pages = sorted(
        set(range(1, total_pdf_pages + 1)) - {row["page"] for row in page_rows if isinstance(row["page"], int)}
    )
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "run": run,
            "pages": page_rows,
            "missing_pages": missing_pages,
            "chunk_total": len(chunks),
            "has_chunks_file": bool(chunks),
        },
    )
