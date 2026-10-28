"""本地向量与真实 tokenizer 预算；导入模块不会加载或下载模型。"""

from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path
from typing import Any

from app import config


@lru_cache(maxsize=1)
def get_model():
    # 延迟导入让解析/契约单测无需初始化 torch；默认只读本地缓存，缺模型显式报错。
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(
        config.EMBEDDING_MODEL,
        device="cpu",
        local_files_only=getattr(config, "EMBEDDING_LOCAL_ONLY", True),
        revision=getattr(config, "EMBEDDING_REVISION", None) or None,
    )


def count_tokens(text: str) -> int:
    """包含特殊 token 的真实长度，显式禁止 tokenizer 自动截断。"""
    encoded = get_model().tokenizer(text, add_special_tokens=True, truncation=False)
    return len(encoded["input_ids"])


def get_token_budget() -> int:
    maximum = int(get_model().max_seq_length)
    configured = int(getattr(config, "EMBEDDING_MAX_TOKENS", maximum))
    budget = min(maximum, configured)
    if budget <= 0:
        raise ValueError("Embedding token预算必须大于0")
    return budget


def _local_model_fingerprint(path: Path) -> str | None:
    if not path.is_dir():
        return None
    digest = hashlib.sha256()
    # 本地模型没有远端commit时，权重和tokenizer内容共同决定身份，不能仅用目录名。
    files = sorted(p for p in path.rglob("*") if p.is_file() and p.suffix in {
        ".json", ".txt", ".model", ".bin", ".safetensors"
    })
    for file in files:
        digest.update(file.relative_to(path).as_posix().encode("utf-8"))
        with file.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


@lru_cache(maxsize=1)
def get_embedding_signature() -> dict[str, Any]:
    model = get_model()
    first = model._first_module()
    auto_config = getattr(getattr(first, "auto_model", None), "config", None)
    commit = getattr(auto_config, "_commit_hash", None)
    tokenizer = model.tokenizer
    backend = getattr(tokenizer, "backend_tokenizer", None)
    tokenizer_state = backend.to_str() if backend is not None else json.dumps(
        tokenizer.get_vocab(), sort_keys=True, ensure_ascii=False
    )
    payload = {
        "model": config.EMBEDDING_MODEL,
        "revision": getattr(config, "EMBEDDING_REVISION", None) or commit,
        "local_files_sha256": _local_model_fingerprint(Path(config.EMBEDDING_MODEL)),
        "dimension": int(model.get_sentence_embedding_dimension()),
        "normalize_embeddings": True,
        "max_seq_length": int(model.max_seq_length),
        "token_budget": get_token_budget(),
        "tokenizer_sha256": hashlib.sha256(tokenizer_state.encode("utf-8")).hexdigest(),
        "text_strategy": "typed-context-v1",
    }
    if not payload["revision"] and not payload["local_files_sha256"]:
        # 离线缓存通常包含commit；未知权重身份不得混进已有向量集合。
        raise ValueError("无法确定Embedding模型版本，请配置EMBEDDING_REVISION或使用本地模型目录")
    payload["signature"] = hashlib.sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return payload


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    budget = get_token_budget()
    for index, text in enumerate(texts):
        actual = count_tokens(text)
        if actual > budget:
            # SentenceTransformer默认可能静默截断；在进入encode前明确阻断。
            raise ValueError(f"Embedding文本超出token预算: index={index}, tokens={actual}, limit={budget}")
    vectors = get_model().encode(
        texts, normalize_embeddings=True, show_progress_bar=False,
        batch_size=max(1, int(getattr(config, "INDEX_BATCH_SIZE", 16))),
    )
    return vectors.tolist()


def embed_query(query: str) -> list[float]:
    return embed_texts([query])[0]
