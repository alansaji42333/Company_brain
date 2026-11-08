from __future__ import annotations

import logging
import os

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


def stored_chunk_id(org_id: str, doc_id: str, chunk_index: int) -> str:
    """The stored Chroma id for a chunk in `org_id` with this doc/index.

    This is the SINGLE derivation of stored vector ids — add_chunks uses it
    (via _chunk_id), and any code deleting/appending vectors for a specific
    chunk must build its id here rather than string-formatting it locally.
    """
    return f"{org_id}_{doc_id}_{chunk_index}"


def _chunk_id(c: dict) -> str:
    org_id = c.get("org_id", "") or ""
    if org_id:
        return stored_chunk_id(org_id, c["doc_id"], c["chunk_index"])
    user_id = c.get("user_id", "")
    return stored_chunk_id(user_id, c["doc_id"], c["chunk_index"])


def add_chunks(chunks: list[dict], collection: str = COLLECTION_NAME):
    col = _get_collection(collection)
    if not chunks:
        return

    ids = [_chunk_id(c) for c in chunks]
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


def query(question: str, top_k: int | None = None, collection: str = COLLECTION_NAME, org_id: str | None = None) -> list[dict]:
    col = _get_collection(collection)
    k = top_k or TOP_K_RETRIEVAL
    [question_embedding] = embed([question])

    kwargs = {
        "query_embeddings": [question_embedding],
        "n_results": k,
        "include": ["documents", "metadatas", "distances"],
        # TODO(per-document ACL): this is a coarse org-level filter. A later
        # step should add per-document ACL filtering (e.g. doc_id allow-lists
        # per user/group) on top of the org scope.
        "where": {"org_id": org_id} if org_id else None,
    }

    results = col.query(**kwargs)

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


def query_both(question: str, top_k: int | None = None, org_id: str | None = None, user_id: str | None = None) -> dict:
    """Retrieve across raw docs and approved playbooks, scoped to an org.

    `user_id` is accepted for future provenance/ACL use and is not part of the
    filter today.
    """
    k = top_k or TOP_K_RETRIEVAL
    raw_chunks = query(question, top_k=k, collection=COLLECTION_NAME, org_id=org_id)
    skill_chunks = query(question, top_k=TOP_K_SKILLS, collection=COLLECTION_SKILLS, org_id=org_id)
    return {"raw": raw_chunks, "skills": skill_chunks}


def update_metadata(ids: list[str], metadatas: list[dict], collection: str = COLLECTION_NAME):
    """Update metadata for existing vectors (used by the org backfill script).

    Only updates the stored metadata; ChromaDB ids cannot be renamed, so an
    org-scoped re-ingest is the clean path to dedupe legacy ids.
    """
    col = _get_collection(collection)
    if not ids:
        return
    col.update(ids=ids, metadatas=metadatas)
    logger.info("Updated metadata for %d id(s) in %s", len(ids), collection)


def delete_by_ids(ids: list[str], collection: str = COLLECTION_NAME):
    col = _get_collection(collection)
    existing = col.get(ids=ids)
    if existing["ids"]:
        col.delete(ids=existing["ids"])
        logger.info("Deleted %d document(s) from %s", len(existing["ids"]), collection)


def get_skill_doc_count() -> int:
    col = _get_collection(COLLECTION_SKILLS)
    return col.count()