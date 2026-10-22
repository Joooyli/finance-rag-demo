from functools import lru_cache
from sentence_transformers import SentenceTransformer
from app import config

@lru_cache(maxsize=1)
def get_model() -> SentenceTransformer:
    # This model runs locally; no embedding API call is made.
    return SentenceTransformer(config.EMBEDDING_MODEL, device="cpu")

def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    vectors = get_model().encode(
        texts,
        normalize_embeddings=True,
        show_progress_bar=False,
        batch_size=16,
    )
    return vectors.tolist()

def embed_query(query: str) -> list[float]:
    return embed_texts([query])[0]
