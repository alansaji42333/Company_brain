import os
import logging
import chromadb
from chromadb.config import Settings
from app.config import CHROMA_DIR, COLLECTION_NAME, COLLECTION_SKILLS, TOP_K_RETRIEVAL, TOP_K_SKILLS
from app.embeddings import embed

logger = logging.getLogger(__name__)

_client: chromadb.PersistentClient | None = None
_collections: dict[str, chromadb.Collection] = {}


def _get_client():
    global _client
    if _client is None:
        os.makedirs(CHROMA_DIR, exist_ok=True)
        _client = chromadb.PersistentClient(path=CHROMA_DIR, settings=Settings(anonymized_telemetry=False))
    return _client


def _get_collection(name: str = COLLECTION_NAME) -> chromadb.Collection:
    if name not in _collections:
        client = _get_client()
        _collections[name] = client.get_or_create_collection(name=name)
    return _collections[name]


def add_chunks(chunks: list[dict], collection: str = COLLECTION_NAME):
    col = _get_collection(collection)
    if not chunks:
        return

    ids = [f"{c['doc_id']}_{c['chunk_index']}" for c in chunks]
    texts = [c["text"] for c in chunks]
    metadatas = [
        {k: v for k, v in c.items() if k != "text"}
        for c in chunks
    ]

    logger.info("Embedding %d chunk(s) into %s...", len(chunks), collection)
    embeddings = embed(texts)

    col.upsert(
        ids=ids,
        embeddings=embeddings,
        documents=texts,
        metadatas=metadatas,
    )
    logger.info("Stored %d chunk(s) in %s", len(chunks), collection)


def query(question: str, top_k: int | None = None, collection: str = COLLECTION_NAME) -> list[dict]:
    col = _get_collection(collection)
    k = top_k or TOP_K_RETRIEVAL
    [question_embedding] = embed([question])

    results = col.query(
        query_embeddings=[question_embedding],
        n_results=k,
        include=["documents", "metadatas", "distances"],
    )

    chunks = []
    if results["ids"] and results["ids"][0]:
        for i, doc_id in enumerate(results["ids"][0]):
            chunks.append({
                "id": doc_id,
                "text": results["documents"][0][i],
                "metadata": results["metadatas"][0][i],
                "score": results["distances"][0][i],
            })
    return chunks


def query_both(question: str, top_k: int | None = None) -> dict:
    k = top_k or TOP_K_RETRIEVAL
    raw_chunks = query(question, top_k=k, collection=COLLECTION_NAME)
    skill_chunks = query(question, top_k=TOP_K_SKILLS, collection=COLLECTION_SKILLS)
    return {"raw": raw_chunks, "skills": skill_chunks}


def delete_by_ids(ids: list[str], collection: str = COLLECTION_NAME):
    col = _get_collection(collection)
    existing = col.get(ids=ids)
    if existing["ids"]:
        col.delete(ids=existing["ids"])
        logger.info("Deleted %d document(s) from %s", len(existing["ids"]), collection)


def get_skill_doc_count() -> int:
    col = _get_collection(COLLECTION_SKILLS)
    return col.count()
