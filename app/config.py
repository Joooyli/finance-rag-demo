import os
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

def env_path(name: str, default: str) -> Path:
    p = Path(os.getenv(name, default))
    return p if p.is_absolute() else ROOT_DIR / p

PDF_DIR = env_path("PDF_DIR", "data/source")
CHROMA_DIR = env_path("CHROMA_DIR", "data/chroma_db")
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "finance_pdf_chunks")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "").strip()
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "700"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "100"))
RETRIEVAL_TOP_K = int(os.getenv("RETRIEVAL_TOP_K", "5"))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "9000"))
ENABLE_OCR = os.getenv("ENABLE_OCR", "true").lower() in {"1", "true", "yes"}
OCR_LANG = os.getenv("OCR_LANG", "chi_sim+eng")
TESSERACT_CMD = os.getenv("TESSERACT_CMD", "").strip()
OCR_LOW_TEXT_PAGES = os.getenv("OCR_LOW_TEXT_PAGES", "true").lower() in {"1", "true", "yes"}
MIN_NATIVE_TEXT_CHARS = int(os.getenv("MIN_NATIVE_TEXT_CHARS", "80"))

def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------- 解析器（pymupdf 2.x）
# 页眉/页脚：广告类 PDF 的页眉多为重复宣传语，默认在解析阶段直接剔除。
HEADER_REMOVE = env_bool("HEADER_REMOVE", True)
FOOTER_REMOVE = env_bool("FOOTER_REMOVE", False)
# 版面分析（PyMuPDF Layout）：关闭后拿不到 page_boxes，elements 为空。
LAYOUT_ENABLED = env_bool("LAYOUT_ENABLED", True)
# markdown 表格识别策略：lines_strict / lines / text
TABLE_STRATEGY = os.getenv("TABLE_STRATEGY", "lines_strict").strip() or "lines_strict"
# OCR 渲染精度，仅作用于被判定需要 OCR 的页面
OCR_DPI = int(os.getenv("OCR_DPI", "300"))

# 解析日志中 elements 的文本精度
PARSE_LOG_INCLUDE_ELEMENTS = env_bool("PARSE_LOG_INCLUDE_ELEMENTS", True)
PARSE_LOG_ELEMENT_TEXT_MAX = int(os.getenv("PARSE_LOG_ELEMENT_TEXT_MAX", "200"))

# 解析与入库分两步执行：默认只解析并落盘，人工检查通过后再开启入库。
ENABLE_INDEXING = env_bool("ENABLE_INDEXING", False)
INDEX_BATCH_SIZE = int(os.getenv("INDEX_BATCH_SIZE", "32"))

# 中间解析产物（每页一行 pages.jsonl、每个切块一行 chunks.jsonl）
PARSE_LOG_ENABLED = env_bool("PARSE_LOG_ENABLED", True)
PARSE_LOG_DIR = env_path("PARSE_LOG_DIR", "data/parse_log")

# 运行日志
LOG_DIR = env_path("LOG_DIR", "data/logs")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
LOG_TO_FILE = env_bool("LOG_TO_FILE", True)
