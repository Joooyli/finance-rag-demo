import os
import json
import re
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

def env_path(name: str, default: str) -> Path:
    p = Path(os.getenv(name, default))
    return p if p.is_absolute() else ROOT_DIR / p

PDF_DIR = env_path("PDF_DIR", "data/source")
CHROMA_DIR = env_path("CHROMA_DIR", "data/chroma_db")
# 新链路单独使用v3集合，避免旧.env中的集合名把新旧schema混在一起。
# LEGACY_COLLECTION_NAME = os.getenv("COLLECTION_NAME", "finance_pdf_chunks")
COLLECTION_NAME = os.getenv("HYBRID_COLLECTION_NAME", "finance_pdf_chunks_v3")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
EMBEDDING_REVISION = os.getenv("EMBEDDING_REVISION", "").strip()
EMBEDDING_LOCAL_ONLY = os.getenv("EMBEDDING_LOCAL_ONLY", "true").lower() in {"1", "true", "yes"}
EMBEDDING_MAX_TOKENS = int(os.getenv("EMBEDDING_MAX_TOKENS", "512"))
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
# 页眉只在所有引擎结果齐备后统一清洗一次。顶部比例仅限定候选范围，不直接裁页。
HEADER_FILTER_TOP_RATIO = float(os.getenv("HEADER_FILTER_TOP_RATIO", "0.12"))
HEADER_REPEAT_MIN_PAGES = int(os.getenv("HEADER_REPEAT_MIN_PAGES", "3"))
HEADER_REPEAT_MIN_RATIO = float(os.getenv("HEADER_REPEAT_MIN_RATIO", "0.6"))
HEADER_REPEAT_POSITION_TOLERANCE = float(os.getenv("HEADER_REPEAT_POSITION_TOLERANCE", "0.02"))
_header_patterns = json.loads(os.getenv("HEADER_AD_PATTERNS", "[]"))
if not isinstance(_header_patterns, list):
    raise ValueError("HEADER_AD_PATTERNS必须是JSON数组")
HEADER_AD_PATTERNS = tuple(_header_patterns)
if not (0 < HEADER_FILTER_TOP_RATIO <= 0.3 and HEADER_REPEAT_MIN_PAGES >= 2
        and 0 < HEADER_REPEAT_MIN_RATIO <= 1 and 0 <= HEADER_REPEAT_POSITION_TOLERANCE <= 0.1):
    raise ValueError("页眉清洗的顶部比例、重复页数/比例或位置容差无效")
if not all(isinstance(pattern, str) and pattern for pattern in HEADER_AD_PATTERNS):
    raise ValueError("HEADER_AD_PATTERNS必须是非空正则字符串组成的JSON数组")
for _header_pattern in HEADER_AD_PATTERNS:
    re.compile(_header_pattern)
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

# MinerU保持独立环境；这些值只注入子进程，不修改机器环境。
MINERU_PYTHON = env_path("MINERU_PYTHON", r"E:\learn_space\mineru\env\.venv\Scripts\python.exe")
MINERU_MODEL_BASE_DIR = env_path("MINERU_MODEL_BASE_DIR", r"E:\learn_space\mineru\model")
MINERU_TIER = "standard"
MINERU_ENABLED = env_bool("MINERU_ENABLED", True)
MINERU_TIMEOUT_SECONDS = int(os.getenv("MINERU_TIMEOUT_SECONDS", "1800"))
MINERU_MAX_CONCURRENCY = 1
MINERU_CACHE_DIR = env_path("MINERU_CACHE_DIR", "data/parser_cache/mineru")
DOCUMENT_DIR = env_path("DOCUMENT_DIR", "data/documents")
INDEX_REGISTRY_PATH = env_path("INDEX_REGISTRY_PATH", "data/index_registry.sqlite3")
PARSE_JOBS_DIR = env_path("PARSE_JOBS_DIR", "data/parse_jobs")
OCR_TIMEOUT_SECONDS = int(os.getenv("OCR_TIMEOUT_SECONDS", "120"))
ROUTING_COMPLEX_AREA_RATIO = float(os.getenv("ROUTING_COMPLEX_AREA_RATIO", "0.35"))
MAX_INVALID_CHAR_RATIO = float(os.getenv("MAX_INVALID_CHAR_RATIO", "0.01"))
ROUTING_RULE_VERSION = "hybrid-v1"
PARSE_ARTIFACT_SCHEMA = 3
# 人工确认是入库约束，不提供环境变量绕过开关。
REVIEW_REQUIRED = True
