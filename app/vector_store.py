from __future__ import annotations
import chromadb
from chromadb.config import Settings
from app import config

def get_collection():
    config.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(
        path=str(config.CHROMA_DIR),
        settings=Settings(anonymized_telemetry=False),
    )
    return client.get_or_create_collection(
        name=config.COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

def upsert_chunks(ids, documents, metadatas, embeddings):
    collection = get_collection()
    # Chroma upsert means re-indexing the same IDs updates rather than duplicates.
    collection.upsert(
        ids=ids,
        documents=documents,
        metadatas=metadatas,
        embeddings=embeddings,
    )
    return collection.count()

def search_chunks(query_embedding, top_k: int):
    collection = get_collection()
    if collection.count() == 0:
        return []
    result = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(top_k, collection.count()),
        include=["documents", "metadatas", "distances"],
    )
    hits = []
    for i, doc in enumerate(result["documents"][0]):
        metadata = result["metadatas"][0][i] or {}
        distance = result["distances"][0][i]
        hits.append({
            "text": doc,
            "source": metadata.get("source", "unknown"),
            "page": metadata.get("page", -1),
            "chunk_id": metadata.get("chunk_id", ""),
            "distance": float(distance),
            # For cosine distance, a larger similarity is better. This is a convenient
            # display value, not a calibrated probability.
            "similarity": round(1.0 - float(distance), 4),
        })
    return hits

def collection_count() -> int:
    return get_collection().count()
