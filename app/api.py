from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app import config
from app.document_source import prepare_source
from app.logging_config import configure_logging
from app.parse_jobs import get_job_manager, shutdown_job_manager
from app.parse_log import load_run
from app.parse_review import approve_run, validate_indexable_run
from app.rag_pipeline import answer_question, retrieve
from app.vector_store import collection_count
from app.web_ui import router as ui_router

configure_logging()


@asynccontextmanager
async def lifespan(app):
    # 单进程本机worker在服务启动时占有目录锁，关闭时协作取消解析并清理子进程。
    get_job_manager()
    try:
        yield
    finally:
        await run_in_threadpool(shutdown_job_manager)


app = FastAPI(title="Local Finance PDF RAG", version="2.0.0", lifespan=lifespan,
    description="本地混合解析→质量检查→人工审阅→本地向量入库；云端OpenAI兼容问答。")
app.include_router(ui_router)


class SearchRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)


class ChatRequest(SearchRequest):
    pass


class ReviewRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=100)
    decision: str = Field(default="approved", pattern="^(approved|rejected)$")
    notes: str = Field(default="", max_length=10000)


@app.get("/health")
def health():
    return {"status": "ok", "collection": config.COLLECTION_NAME,
            # 只计活动快照，不能把staged/历史版本当作当前知识库。
            "indexed_chunks": collection_count(),
            "llm_configured": bool(config.LLM_API_KEY and config.LLM_BASE_URL and config.LLM_MODEL),
            "embedding_model": config.EMBEDDING_MODEL, "indexing_enabled": config.ENABLE_INDEXING,
            "parse_log_enabled": config.PARSE_LOG_ENABLED, "review_required": True,
            "all_pdf_and_vector_processing_local": True}


@app.post("/documents/index", status_code=202)
async def documents_index(file: UploadFile = File(...),
    index: bool | None = Query(default=None, description="记录入库意图；新解析仍必须人工确认后显式入库"),
    doc_id: str | None = Query(default=None, description="更新已有文档时显式指定；省略则新增文档")):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="请上传PDF文件")
    safe_name = Path(file.filename.replace("\\", "/")).name
    upload_dir = Path(config.PARSE_JOBS_DIR) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        # 临时上传不放进data/source，避免被批量解析脚本当成已完整上传的原件。
        with tempfile.NamedTemporaryFile(suffix=".pdf", dir=upload_dir, delete=False) as handle:
            temporary = Path(handle.name)
            first = await file.read(1024 * 1024)
            if not first.startswith(b"%PDF"):
                raise HTTPException(status_code=400, detail="文件内容不是有效的PDF头")
            handle.write(first)
            while part := await file.read(1024 * 1024):
                handle.write(part)
        source_info = await run_in_threadpool(prepare_source, temporary, doc_id=doc_id,
            source_key=f"upload:{uuid4().hex}", source=safe_name)
        requested = config.ENABLE_INDEXING if index is None else index
        job = get_job_manager().submit_parse(source_info, index_requested=requested)
        return {"status": "queued", "job_id": job["job_id"], "doc_id": source_info.doc_id,
                "source": source_info.source, "index_requested": requested,
                "note": "已提交解析任务；完成后请检查并人工确认，再显式提交入库。"}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        await file.close()


@app.get("/jobs")
def jobs():
    return {"jobs": get_job_manager().list_jobs()}


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    job = get_job_manager().get_status(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    try:
        return get_job_manager().cancel(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/runs/{run_id}")
def run_status(run_id: str):
    try:
        run = load_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if run is None:
        raise HTTPException(status_code=404, detail="解析记录不存在")
    return run


@app.post("/runs/{run_id}/review")
def review_run(run_id: str, request: ReviewRequest):
    try:
        return approve_run(run_id, reviewer=request.reviewer, decision=request.decision,
                           notes=request.notes)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/runs/{run_id}/index", status_code=202)
def index_run(run_id: str):
    try:
        # 排队前及真正写入前都校验，审阅后内容若被修改也不能进入向量库。
        validate_indexable_run(run_id)
        job = get_job_manager().submit_index(run_id)
        return {"status": "queued", "job_id": job["job_id"], "run_id": run_id}
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/retrieval/search")
def retrieval_search(request: SearchRequest):
    try:
        hits = retrieve(request.question, request.top_k)
        return {"question": request.question, "count": len(hits), "results": hits}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/chat")
def chat(request: ChatRequest):
    try:
        return answer_question(request.question, request.top_k)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
