"""ARQ worker configuration.

Registers the ingestion and synthesis coroutines as background jobs and
exposes the WorkerSettings used by `arq app.worker.WorkerSettings`.

The job functions here are thin async wrappers around the existing sync
ingestion pipelines (drive_ingest, slack_ingest, skill_synthesis). They run
inside the worker process, off the request thread, so HTTP endpoints return
immediately with a job_id.
"""
import logging
from typing import Any, ClassVar

from arq.connections import RedisSettings

from app.config import REDIS_URL

logger = logging.getLogger(__name__)


def redis_settings() -> RedisSettings:
    """Build ARQ RedisSettings from the configured REDIS_URL."""
    return RedisSettings.from_dsn(REDIS_URL)


# --- registered job functions ------------------------------------------
# Each must accept the ARQ context (`ctx`) as the first argument.

async def ingest_drive(
    ctx: dict, folder_id: str | None = None, user_id: str = "", org_id: str = "",
) -> dict:
    """Ingest a Google Drive folder fresh-first, per document:

    - unchanged (same content_hash) → skip entirely (no re-embed, no delete);
    - changed → delete the doc's stored vectors, re-chunk + re-embed (`updated`);
    - new → chunk + embed (`ingested`);
    - gone from the folder listing → vectors deleted (`deleted`).
    """
    from app import vectorstore
    from app.chunking import chunk_document, content_hash
    from app.drive_ingest import ingest_drive_folder

    logger.info("job ingest_drive: folder_id=%s org_id=%s user_id=%s", folder_id, org_id, user_id)
    documents = ingest_drive_folder(folder_id, user_id=user_id, org_id=org_id)
    folder_doc_ids = {doc["id"] for doc in documents}

    counts = {
        "files_processed": len(documents),
        "ingested": 0,
        "updated": 0,
        "skipped": 0,
        "deleted": 0,
        "chunks_stored": 0,
    }

    for doc in documents:
        doc_id = doc["id"]
        new_hash = content_hash(doc["text"])
        stored = vectorstore.get_doc_chunk_metadatas(doc_id, org_id)
        stored_hashes = {m.get("content_hash") for m in stored}

        if stored and stored_hashes == {new_hash}:
            counts["skipped"] += 1
            continue

        was_stored = bool(stored)
        if was_stored:
            # Replace: content changed — drop the old vectors first so the
            # fresh set is the only set (chunk count may differ).
            vectorstore.delete_by_doc_id(doc_id, org_id)

        chunks = chunk_document(doc, user_id=user_id, org_id=org_id)
        vectorstore.add_chunks(chunks)
        counts["chunks_stored"] += len(chunks)
        counts["updated" if was_stored else "ingested"] += 1

    # Deletion sweep: any drive doc still stored for this org but absent from
    # the folder listing no longer exists at the source — remove its vectors.
    # list_doc_ids/delete_by_doc_id filter on org_id, so this can never touch
    # another org's chunks (even with identical doc_ids).
    if org_id:
        stored_doc_ids = vectorstore.list_doc_ids(org_id, "drive")
        for gone_doc_id in sorted(stored_doc_ids - folder_doc_ids):
            vectorstore.delete_by_doc_id(gone_doc_id, org_id)
            counts["deleted"] += 1
            logger.info("Swept deleted drive doc %s (org %s)", gone_doc_id, org_id)

    logger.info("job ingest_drive done: %s", counts)
    return counts


async def ingest_slack(ctx: dict, user_id: str = "", org_id: str = "") -> dict:
    """Ingest Slack channels with WHOLESALE REPLACE per channel.

    slack_ingest.ingest_slack deletes each channel's stored chunks
    (slack_{channel_id}, org-scoped) and returns the fresh chunks; storing
    them here completes the replace. This drops deleted messages,
    regrouped conversation windows, and orphaned higher-index chunks in one
    move.
    """
    from app import vectorstore
    from app.slack_ingest import ingest_slack as run_ingest

    logger.info("job ingest_slack: org_id=%s user_id=%s", org_id, user_id)
    per_channel = run_ingest(user_id=user_id, org_id=org_id)

    counts = {"channels": len(per_channel), "chunks_stored": 0, "chunks_deleted": 0}
    for channel_id, payload in per_channel.items():
        chunks = payload["chunks"]
        vectorstore.add_chunks(chunks)
        counts["chunks_stored"] += len(chunks)
        counts["chunks_deleted"] += payload["chunks_deleted"]
        logger.info(
            "job ingest_slack: channel %s replaced (%d stored, %d deleted)",
            channel_id, len(chunks), payload["chunks_deleted"],
        )

    logger.info("job ingest_slack done: %s", counts)
    return counts


async def synthesize(ctx: dict, user_id: str = "", org_id: str = "") -> dict:
    """Run skill synthesis over newly ingested chunks for one org."""
    from app.skill_synthesis import synthesize as run_synthesis

    logger.info("job synthesize: org_id=%s user_id=%s", org_id, user_id)
    summary = run_synthesis(org_id=org_id)
    logger.info("job synthesize done: %s", summary)
    return summary


# --- worker entrypoint --------------------------------------------------

class WorkerSettings:
    """Configuration consumed by `arq app.worker.WorkerSettings`."""
    functions: ClassVar[list[Any]] = [ingest_drive, ingest_slack, synthesize]
    redis_settings = redis_settings()
    max_jobs = 10
    job_timeout = 600
    max_tries = 3