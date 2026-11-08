"""Hermetic source-freshness tests: incremental ingest, change + deletion.

No network, no real Chroma/Redis/Postgres, no embedding model: the Chroma
collection surface is replaced by an in-memory fake collection, `embed` by a
recording stub, and the Drive/Slack pipelines by monkeypatched functions at
their seams (same style as tests/test_ingest_jobs.py and conftest env).
"""
import pytest


def make_drive_doc(doc_id: str, text: str, org_id: str = "org_tenant1", name: str | None = None) -> dict:
    """A Drive document in the shape extract_text() produces."""
    return {
        "id": doc_id,
        "name": name or f"{doc_id}.doc",
        "mime_type": "text/plain",
        "text": text,
        "source_url": "https://drive.test/file/" + doc_id,
        "source_type": "drive",
    }


def make_chunk(doc_id: str, chunk_index: int, text: str, org_id: str = "org_tenant1", doc_name: str | None = None) -> dict:
    """A Slack-style chunk in the shape conversation_to_chunks() produces."""
    return {
        "doc_id": doc_id,
        "doc_name": doc_name or "remote",
        "source_url": "",
        "chunk_index": chunk_index,
        "text": text,
        "source_type": "slack",
        "user_id": "",
        "org_id": org_id,
    }


class FakeCollection:
    """In-memory stand-in for the Chroma collection surface vectorstore uses:

    get(ids=, where=, include=), delete(ids), upsert(...), count().
    """

    def __init__(self):
        self.docs: dict[str, dict] = {}  # id -> {"document": str, "metadata": dict}

    def get(self, ids=None, where=None, include=None):
        selected = list(self.docs.items())
        if ids is not None:
            wanted = set(ids)
            selected = [(i, d) for i, d in selected if i in wanted]
        if where:
            def matches(metadata: dict) -> bool:
                return all(metadata.get(k) == v for k, v in where.items())

            selected = [(i, d) for i, d in selected if matches(d["metadata"])]
        return {
            "ids": [i for i, _ in selected],
            "metadatas": [d["metadata"] for _, d in selected],
            "documents": [d["document"] for _, d in selected],
        }

    def delete(self, ids):
        for i in ids:
            self.docs.pop(i, None)

    def upsert(self, ids, embeddings, documents, metadatas):
        for i, doc, meta in zip(ids, documents, metadatas):
            self.docs[i] = {"document": doc, "metadata": meta}

    def count(self) -> int:
        return len(self.docs)


@pytest.fixture
def fake_store(monkeypatch):
    """Replace the vectorstore's Chroma collections + embed with fakes.

    Returns (collections: dict[name -> FakeCollection], embed_calls: list).
    `collections` is keyed like the real code (name -> collection);
    `fake_store[collection_name].docs` is the raw id -> content map.
    """
    from app import vectorstore

    collections: dict[str, FakeCollection] = {}

    def fake_get_collection(name=vectorstore.COLLECTION_NAME):
        return collections.setdefault(name, FakeCollection())

    embed_calls: list[list[str]] = []

    def fake_embed(texts):
        embed_calls.append(list(texts))
        return [[0.0] * 4 for _ in texts]

    monkeypatch.setattr(vectorstore, "_get_collection", fake_get_collection)
    monkeypatch.setattr(vectorstore, "embed", fake_embed)
    # Pre-create the main collection so tests can index into it directly.
    collections.setdefault(vectorstore.COLLECTION_NAME, FakeCollection())
    return collections, embed_calls


# =====================================================================
# 1. vectorstore document-level helpers
# =====================================================================

def test_delete_by_doc_id_removes_all_chunks_and_reports_count(fake_store):
    from app import vectorstore

    collections, _ = fake_store
    fake = collections[vectorstore.COLLECTION_NAME]
    for idx in range(3):
        fake.docs[vectorstore.stored_chunk_id("orgA", "doc1", idx)] = {
            "document": f"chunk {idx}",
            "metadata": {"doc_id": "doc1", "org_id": "orgA", "chunk_index": idx},
        }

    deleted = vectorstore.delete_by_doc_id("doc1", "orgA")
    assert deleted == 3
    assert fake.count() == 0


def test_delete_by_doc_id_is_org_scoped(fake_store):
    from app import vectorstore

    collections, _ = fake_store
    fake = collections[vectorstore.COLLECTION_NAME]
    for org in ("orgA", "orgB"):
        idx = 0
        fake.docs[vectorstore.stored_chunk_id(org, "same_doc_id", idx)] = {
            "document": f"content-{org}",
            "metadata": {"doc_id": "same_doc_id", "org_id": org, "chunk_index": idx},
        }

    deleted = vectorstore.delete_by_doc_id("same_doc_id", "orgA")
    assert deleted == 1
    remaining = fake.get(where={"org_id": "orgB"})
    assert remaining["ids"] == [vectorstore.stored_chunk_id("orgB", "same_doc_id", 0)]


def test_get_doc_chunk_metadatas_returns_only_that_orgs_doc(fake_store):
    from app import vectorstore

    collections, _ = fake_store
    fake = collections[vectorstore.COLLECTION_NAME]
    fake.docs["a"] = {
        "document": "a",
        "metadata": {"doc_id": "doc1", "org_id": "orgA", "source_type": "drive", "chunk_index": 0},
    }
    fake.docs["b"] = {
        "document": "b",
        "metadata": {"doc_id": "doc1", "org_id": "orgB", "source_type": "drive", "chunk_index": 0},
    }

    metas = vectorstore.get_doc_chunk_metadatas("doc1", "orgA")
    assert len(metas) == 1
    assert metas[0] == {"doc_id": "doc1", "org_id": "orgA", "source_type": "drive", "chunk_index": 0}


def test_list_doc_ids_dedupes_per_org_and_source_type(fake_store):
    from app import vectorstore

    collections, _ = fake_store
    fake = collections[vectorstore.COLLECTION_NAME]
    for idx, doc_id in enumerate(["doc1", "doc1", "doc2"]):
        fake.docs[vectorstore.stored_chunk_id("orgA", doc_id, idx)] = {
            "document": f"chunk {idx}",
            "metadata": {"doc_id": doc_id, "org_id": "orgA", "source_type": "drive", "chunk_index": idx},
        }
    fake.docs["slack"] = {
        "document": "slack chunk",
        "metadata": {"doc_id": "slack_C123", "org_id": "orgA", "source_type": "slack", "chunk_index": 0},
    }
    fake.docs["orgB"] = {
        "document": "orgB chunk",
        "metadata": {"doc_id": "doc1", "org_id": "orgB", "source_type": "drive", "chunk_index": 0},
    }

    drive_ids = vectorstore.list_doc_ids("orgA", "drive")
    assert drive_ids == {"doc1", "doc2"}

    assert vectorstore.list_doc_ids("orgA", "slack") == {"slack_C123"}
    assert vectorstore.list_doc_ids("orgB", "drive") == {"doc1"}
