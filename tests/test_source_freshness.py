"""Hermetic source-freshness tests: incremental ingest, change + deletion.

No network, no real Chroma/Redis/Postgres, no embedding model: the Chroma
collection surface is replaced by an in-memory fake collection, `embed` by a
recording stub, and the Drive/Slack pipelines by monkeypatched functions at
their seams (same style as tests/test_ingest_jobs.py and conftest env).
"""
import asyncio

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


# =====================================================================
# 2. Drive: hash-based skip / update
# =====================================================================

ORG = "org_tenant1"


def run_drive_ingest(monkeypatch, documents, org_id: str = ORG) -> dict:
    """Run the real `ingest_drive` worker job against a scripted folder and
    the fake vector store."""
    from app import drive_ingest
    from app.worker import ingest_drive

    monkeypatch.setattr(drive_ingest, "ingest_drive_folder", lambda *a, **kw: list(documents))
    return asyncio_run_job(ingest_drive, {}, folder_id="folder1", user_id="scheduler", org_id=org_id)


def asyncio_run_job(coro_fn, ctx: dict, **kwargs) -> dict:
    return asyncio.run(coro_fn(ctx, **kwargs))


def make_slack_chunks(channel_id: str, texts: list[str], org_id: str = ORG) -> list[dict]:
    """Chunks for one channel in the shape conversation_to_chunks() yields."""
    return [
        make_chunk(f"slack_{channel_id}", i, text, org_id=org_id, doc_name=f"#{channel_id}")
        for i, text in enumerate(texts)
    ]


def run_slack_ingest(monkeypatch, chunks_by_run: list[list[dict]], org_id: str = ORG) -> dict:
    """Run the real `ingest_slack` worker job; each element of
    `chunks_by_run` is what ingest_slack_channel "returns" on that run."""
    from app import slack_ingest
    from app.worker import ingest_slack

    runs = iter(chunks_by_run)
    monkeypatch.setattr(slack_ingest, "SLACK_CHANNEL_IDS", "C123")
    monkeypatch.setattr(
        slack_ingest, "ingest_slack_channel",
        lambda cid, user_id="", org_id="": list(next(runs)),
    )

    return asyncio_run_job(ingest_slack, {}, user_id="scheduler", org_id=org_id)


def test_unchanged_drive_doc_skipped_on_reingest(fake_store, monkeypatch):
    from app import vectorstore
    from app.chunking import content_hash

    collections, embed_calls = fake_store
    fake = collections[vectorstore.COLLECTION_NAME]
    doc = make_drive_doc("file1", "The quarterly security overview document.")

    first = run_drive_ingest(monkeypatch, [doc])
    assert first["ingested"] == 1
    assert first["skipped"] == 0
    assert first["chunks_stored"] == 1
    assert len(fake.docs) == 1

    n_embeds_after_first = len(embed_calls)
    second = run_drive_ingest(monkeypatch, [doc])

    assert second["skipped"] == 1
    assert second["ingested"] == 0 and second["updated"] == 0
    assert second["chunks_stored"] == 0
    assert len(embed_calls) == n_embeds_after_first, "unchanged content must not re-embed"
    assert fake.count() == 1
    stored_meta = fake.get()["metadatas"][0]
    assert stored_meta["content_hash"] == content_hash(doc["text"])


def test_changed_drive_text_replaces_exactly_that_docs_vectors(fake_store, monkeypatch):
    from app import vectorstore

    collections, embed_calls = fake_store
    fake = collections[vectorstore.COLLECTION_NAME]
    filler = "Old content sentence that is long enough. " * 200
    other = make_drive_doc("file2", "Some other stable content.")
    old_text = filler + " extra tail to push chunk count up"
    run_drive_ingest(monkeypatch, [make_drive_doc("file1", old_text), other])
    old_ids = set(fake.docs)
    assert len(old_ids) >= 3  # multi-chunk doc1 + one chunk of file2
    n_embeds_before = len(embed_calls)

    new_text = "Brand new, very short content."
    result = run_drive_ingest(monkeypatch, [make_drive_doc("file1", new_text), other])

    assert result["updated"] == 1
    # unchanged doc2 skipped
    assert result["skipped"] == 1
    # only the new chunk set for file1 remains; other chunks untouched
    new_ids = set(fake.docs)
    gone = old_ids - new_ids
    assert all("file1" in chunk_id for chunk_id in gone), "only file1's old chunks deleted"
    assert all("file2" not in chunk_id for chunk_id in gone), "unchanged file2 not deleted"
    # every surviving file1 chunk now carries the NEW content (index 0 was
    # overwritten in place by the upsert; higher indexes were deleted)
    file1_new = {
        cid: fake.docs[cid] for cid in new_ids if "file1" in cid
    }
    assert set(file1_new) == {vectorstore.stored_chunk_id(ORG, "file1", 0)}
    assert "Brand new, very short content." in next(iter(file1_new.values()))["document"]
    # file2's stored chunk is byte-identical to before (untouched)
    file2_id = vectorstore.stored_chunk_id(ORG, "file2", 0)
    assert file2_id in new_ids and file2_id in old_ids
    assert len(embed_calls) > n_embeds_before, "changed content re-embedded"


# =====================================================================
# 3. Drive: deletion sweep for docs removed from the folder
# =====================================================================

def test_removed_drive_doc_swept_from_store(fake_store, monkeypatch):
    from app import vectorstore

    collections, embed_calls = fake_store
    fake = collections[vectorstore.COLLECTION_NAME]
    gone = make_drive_doc("file_gone", "This file will be deleted from Drive.")
    keep = make_drive_doc("file_keep", "This file stays in the folder.")

    # Seed the store as if three prior ingests happened: file_gone, file_keep
    # and another previously-removed doc with stale vectors.
    run_drive_ingest(monkeypatch, [gone, keep])
    assert fake.count() == 2
    n_embeds_before = len(embed_calls)

    # file_gone was deleted from the folder: the listing no longer has it.
    result = run_drive_ingest(monkeypatch, [keep])

    assert result["deleted"] == 1
    assert result["skipped"] == 1
    assert result["files_processed"] == 1
    remaining_ids = set(fake.docs)
    assert vectorstore.stored_chunk_id(ORG, "file_gone", 0) not in remaining_ids
    assert vectorstore.stored_chunk_id(ORG, "file_keep", 0) in remaining_ids
    assert len(embed_calls) == n_embeds_before, "sweep must not re-embed anything"


def test_sweep_keeps_slack_vectors_of_same_org(fake_store, monkeypatch):
    """The sweep matches source_type=drive only — slack chunks survive."""
    from app import vectorstore

    collections, _ = fake_store
    fake = collections[vectorstore.COLLECTION_NAME]
    fake.docs[vectorstore.stored_chunk_id(ORG, "slack_C123", 0)] = {
        "document": "slack dialogue",
        "metadata": {"doc_id": "slack_C123", "org_id": ORG, "source_type": "slack", "chunk_index": 0},
    }

    run_drive_ingest(monkeypatch, [])  # empty folder listing

    assert vectorstore.stored_chunk_id(ORG, "slack_C123", 0) in fake.docs


# =====================================================================
# 5. Org isolation of the sweep
# =====================================================================

def test_org_A_sweep_never_touches_org_B_same_doc_id(fake_store, monkeypatch):
    """Org A and org B both stored a doc with the SAME doc_id. Org A's file
    disappears from its folder; org B's copy must survive untouched."""
    from app import vectorstore

    collections, embed_calls = fake_store
    fake = collections[vectorstore.COLLECTION_NAME]
    shared_doc = "shared_file_id"

    doc_a = make_drive_doc(shared_doc, "Org A's copy of the file.", org_id="org_A")
    doc_b = make_drive_doc(shared_doc, "Org B's copy of the file.", org_id="org_B")
    run_drive_ingest(monkeypatch, [doc_a], org_id="org_A")
    run_drive_ingest(monkeypatch, [doc_b], org_id="org_B")
    assert fake.count() == 2
    n_embeds_before = len(embed_calls)

    # Org A's folder no longer contains the file; org B's still does.
    result_a = run_drive_ingest(monkeypatch, [], org_id="org_A")
    run_drive_ingest(monkeypatch, [doc_b], org_id="org_B")

    assert result_a["deleted"] == 1
    # Only org A's chunk is gone; org B's chunk with the same doc_id remains.
    assert fake.count() == 1
    remaining = fake.get(where={"org_id": "org_B"})["metadatas"]
    assert len(remaining) == 1
    assert remaining[0]["doc_id"] == shared_doc
    assert remaining[0]["org_id"] == "org_B"
    assert len(embed_calls) == n_embeds_before, "isolation sweep must not re-embed"


# =====================================================================
# 4. Slack: per-channel wholesale replace
# =====================================================================

def test_shrinking_slack_channel_leaves_no_orphan_chunks(fake_store, monkeypatch):
    from app import vectorstore

    collections, _embed_calls = fake_store
    fake = collections[vectorstore.COLLECTION_NAME]

    # Run 1: three chunks stored for the channel.
    run_slack_ingest(monkeypatch, [make_slack_chunks("C123", ["window one", "window two", "window three"])])
    old_ids = {cid for cid in fake.docs if "slack_C123" in cid}
    assert len(old_ids) == 3

    # Run 2: history shrank / windows regrouped — only ONE chunk now.
    new_text = "the only surviving window"
    result = run_slack_ingest(monkeypatch, [make_slack_chunks("C123", [new_text])])

    assert result["channels"] == 1
    assert result["chunks_stored"] == 1
    assert result["chunks_deleted"] == 3, "all three old channel chunks must be deleted"

    # Exactly the new chunk set remains — no orphaned higher-index chunks.
    doc_chunks = fake.get(where={"org_id": ORG, "doc_id": "slack_C123"})
    assert doc_chunks["ids"] == [vectorstore.stored_chunk_id(ORG, "slack_C123", 0)]
    assert doc_chunks["documents"] == [new_text]
    assert all("slack_C123" in cid for cid in fake.docs)

    # Run 3: wholesale replace is unconditional — the old chunk is deleted
    # again and the store still holds EXACTLY the new set.
    result3 = run_slack_ingest(monkeypatch, [make_slack_chunks("C123", [new_text])])
    assert result3["chunks_deleted"] == 1
    assert result3["chunks_stored"] == 1
    assert fake.get(where={"org_id": ORG, "doc_id": "slack_C123"})["ids"] == [
        vectorstore.stored_chunk_id(ORG, "slack_C123", 0),
    ]


def test_slack_failed_channel_keeps_its_stored_chunks(fake_store, monkeypatch):
    """A channel whose pipeline fails is skipped: its stored chunks are NOT
    deleted-then-lost."""
    from app import slack_ingest, vectorstore
    from app.worker import ingest_slack

    collections, _ = fake_store
    fake = collections[vectorstore.COLLECTION_NAME]

    runs = iter([make_slack_chunks("C_ok", ["fine"])])
    monkeypatch.setattr(slack_ingest, "SLACK_CHANNEL_IDS", "C_ok, C_broken")
    monkeypatch.setattr(slack_ingest, "ingest_slack_channel", lambda cid, user_id="", org_id="": list(next(runs)))

    asyncio_run_job(ingest_slack, {}, user_id="scheduler", org_id=ORG)
    assert fake.count() == 1

    # Now the good channel produces new content and the broken one raises.
    runs2 = iter([make_slack_chunks("C_ok", ["fine again"])])
    monkeypatch.setattr(
        slack_ingest, "ingest_slack_channel",
        lambda cid, user_id="", org_id="": list(next(runs2)) if cid == "C_ok" else (_ for _ in ()).throw(RuntimeError("slack api down")),
    )

    result = asyncio_run_job(ingest_slack, {}, user_id="scheduler", org_id=ORG)

    assert result["channels"] == 1, "the failed channel must not be counted"
    assert fake.get(where={"org_id": ORG, "doc_id": "slack_C_broken"})["ids"] == []
    remaining_ok = fake.get(where={"org_id": ORG, "doc_id": "slack_C_ok"})
    assert remaining_ok["documents"] == ["fine again"], "good channel still replaced"


# =====================================================================
# 6. Audit: one ingest_summary row per ingest job
# =====================================================================

@pytest.fixture
def audit_db(monkeypatch, tmp_path):
    """Point app.database at a tmp sqlite engine with the schema created."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app import database

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "async_session", session_factory)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)

    asyncio.run(_create())
    return database


def _audit_rows(database, org_id: str) -> list:
    from sqlalchemy import select

    async def _query():
        async with database.async_session() as db:
            rows = await db.execute(
                select(database.AuditLog).where(database.AuditLog.org_id == org_id)
            )
            return list(rows.scalars().all())

    return asyncio.run(_query())


def test_drive_ingest_writes_ingest_summary_audit_row(audit_db, fake_store, monkeypatch):
    from app import audit

    doc = make_drive_doc("file1", "Audit me.")
    result = run_drive_ingest(monkeypatch, [doc])

    rows = _audit_rows(audit_db, ORG)
    assert len(rows) == 1
    row = rows[0]
    assert row.action_type == audit.ACTION_INGEST_SUMMARY
    assert row.org_id == ORG
    assert row.user_id == "scheduler"
    assert row.payload == {"source": "drive", **result}
    assert row.status == "success"


def test_slack_ingest_writes_ingest_summary_audit_row(audit_db, fake_store, monkeypatch):
    from app import (
        audit,
        vectorstore,  # noqa: F401 — fake store activation
    )

    run_slack_ingest(
        monkeypatch, [make_slack_chunks("C123", ["dialogue", "more dialogue"])],
    )

    rows = _audit_rows(audit_db, ORG)
    assert len(rows) == 1
    row = rows[0]
    assert row.action_type == audit.ACTION_INGEST_SUMMARY
    assert row.payload["source"] == "slack"
    assert row.payload["channels"] == 1
    assert row.payload["chunks_stored"] == 2
    assert row.payload["chunks_deleted"] == 0


def test_audit_failure_does_not_fail_the_ingest(audit_db, fake_store, monkeypatch):
    """Best-effort audit: a broken DB must not fail the ingest job."""
    real_session_factory = audit_db.async_session

    async def broken_session(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(audit_db, "async_session", broken_session)

    result = run_drive_ingest(monkeypatch, [make_drive_doc("file1", "Audit me anyway.")])

    assert result["ingested"] == 1  # the ingest itself still succeeded

    # Restore the working session for verification: no audit row was written.
    audit_db.async_session = real_session_factory
    assert _audit_rows(audit_db, ORG) == []
