from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from app import config
from app.logging_config import configure_logging
from app.rag_pipeline import answer_question, index_pdf, retrieve
from app.vector_store import collection_count
from app.web_ui import router as ui_router

configure_logging()

app = FastAPI(
    title="Local Finance PDF RAG",
    version="1.1.0",
    description="本地 PDF 解析/Embedding/Chroma，云端 OpenAI 兼容 LLM。解析与入库可分两步执行。",
)

app.include_router(ui_router)


class SearchRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "collection": config.COLLECTION_NAME,
        "indexed_chunks": collection_count(),
        "llm_configured": bool(config.LLM_API_KEY and config.LLM_BASE_URL and config.LLM_MODEL),
        "embedding_model": config.EMBEDDING_MODEL,
        "indexing_enabled": config.ENABLE_INDEXING,
        "parse_log_enabled": config.PARSE_LOG_ENABLED,
        "all_pdf_and_vector_processing_local": True,
    }


@app.post("/documents/index")
async def documents_index(
    file: UploadFile = File(...),
    index: bool | None = Query(
        default=None,
        description="是否写入向量库；省略时使用 ENABLE_INDEXING。传 false 只解析并写日志。",
    ),
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")
    config.PDF_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename).name
    target = config.PDF_DIR / safe_name
    try:
        content = await file.read()
        if not content.startswith(b"%PDF"):
            raise HTTPException(status_code=400, detail="Uploaded file does not look like a PDF")
        target.write_bytes(content)
        result = index_pdf(target, index=index)
        return {"status": "indexed" if result.get("indexed") else "parsed", **result}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Indexing failed: {exc}") from exc


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
