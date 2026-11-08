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


async def _audit_ingest_summary(org_id: str, user_id: str, payload: dict) -> None:
    """Record one audit ingest_summary row per ingest job (best-effort).

    Worker jobs run outside any request transaction, so this opens its own
    session and commits. A failure to audit must not fail the ingest — log
    and continue.
    """
    from app.audit import ACTION_INGEST_SUMMARY, record
    from app.database import async_session

    try:
        async with async_session() as db:
            await record(db, org_id, user_id, ACTION_INGEST_SUMMARY, payload=payload)
            await db.commit()
    except Exception as e:
        logger.error("Failed to record ingest_summary audit for org %s: %s", org_id, e)


async def _load_org_connector(org_id: str, source: str) -> dict | None:
    """Load and decrypt the org's connector config for `source` (None if absent).

    Runs inside its own short-lived session which is closed before any of the
    (sync, potentially long) ingest work starts. Best-effort: if the DB is
    unreachable or the table is missing (e.g. migration not yet applied, or
    tests exercising the ingest seams without a DB), this degrades to None →
    the caller falls back to the env/file behavior, matching how the process
    always worked. Failures are logged loud (ERROR) so a silent wrong-tenant
    fallback to global credentials is visible to operators.
    """
    from app import connectors
    from app.database import async_session

    try:
        async with async_session() as db:
            return await connectors.get_connector(db, org_id, source)
    except Exception as e:
        logger.error(
            "Failed to load %s connector config for org %s (%s); falling back to env credentials",
            source, org_id, e,
        )
        return None


async def _persist_refreshed_drive_token(org_id: str, user_id: str, new_token_dict: dict) -> None:
    """Re-encrypt and store a refreshed OAuth token for the org (best-effort).

    Google rotates the access token on refresh; without persisting it, the
    next ingest could hit an expired token. A persist failure is logged, not
    raised — the current ingest already succeeded.
    """
    from app import connectors
    from app.database import async_session

    try:
        async with async_session() as db:
            config = await connectors.get_connector(db, org_id, "drive")
            if config is None:
                logger.warning("Drive token refreshed for org %s but no connector row found", org_id)
                return
            config["token_json"] = new_token_dict
            await connectors.set_connector(db, org_id, "drive", config, user_id)
            await db.commit()
        logger.info("Persisted refreshed drive token for org %s", org_id)
    except Exception as e:
        logger.error("Failed to persist refreshed drive token for org %s: %s", org_id, e)


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

    Credentials: the org's stored connector (decrypted) if configured, else
    the original env/file path. A missing connector in both places is NOT an
    ARQ retry — it is a waiting-for-setup state, so the job reports an error
    result instead of raising.
    """
    from app import connectors, vectorstore
    from app import google_auth as google_auth_boundary
    from app.chunking import chunk_document, content_hash
    from app.drive_ingest import ingest_drive_folder

    logger.info("job ingest_drive: folder_id=%s org_id=%s user_id=%s", folder_id, org_id, user_id)

    try:
        org_cfg = await _load_org_connector(org_id, "drive")
        resolved_folder, credentials_json, token_json = connectors.resolve_drive_config(org_cfg, folder_id)
    except ValueError as e:
        # Nothing configured anywhere (or an empty org config): a permanent
        # waiting-for-setup state, not an ARQ retry.
        error = str(e) if org_cfg is not None else f"no drive connector configured for org {org_id}"
        result = {"error": error, "files_processed": 0, "chunks_stored": 0}
        await _audit_ingest_summary(org_id, user_id, {"source": "drive", "error": error})
        return result

    credential_error: str | None = None
    refreshed_token_holder: dict | None = None

    def on_token_refresh(new_token_dict: dict) -> None:
        nonlocal refreshed_token_holder
        refreshed_token_holder = new_token_dict

    try:
        if token_json is not None or credentials_json is not None:
            # Per-org OAuth: headless resolution (never files/env/interactive).
            credentials = google_auth_boundary.get_credentials(
                credentials_json=credentials_json,
                token_dict=token_json,
                on_token_refresh=on_token_refresh,
            )
        else:
            credentials = None  # original env/file path
        documents = ingest_drive_folder(
            resolved_folder, user_id=user_id, org_id=org_id, credentials=credentials,
        )
    except RuntimeError as e:
        # Unusable org OAuth token — permanent until an admin reconnects.
        credential_error = str(e)
        documents = []

    if credential_error:
        result = {"error": credential_error, "files_processed": 0, "chunks_stored": 0}
        await _audit_ingest_summary(org_id, user_id, {"source": "drive", "error": credential_error})
        return result

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

    if refreshed_token_holder is not None:
        await _persist_refreshed_drive_token(org_id, user_id, refreshed_token_holder)

    await _audit_ingest_summary(org_id, user_id, {"source": "drive", **counts})
    logger.info("job ingest_drive done: %s", counts)
    return counts


async def ingest_slack(ctx: dict, user_id: str = "", org_id: str = "") -> dict:
    """Ingest Slack channels with WHOLESALE REPLACE per channel.

    slack_ingest.ingest_slack deletes each channel's stored chunks
    (slack_{channel_id}, org-scoped) and returns the fresh chunks; storing
    them here completes the replace. This drops deleted messages,
    regrouped conversation windows, and orphaned higher-index chunks in one
    move.

    Credentials: the org's stored connector (decrypted) — the org's bot token
    is the WebClient credential for this job and the org's channel_ids are
    the only channels ingested. Without a stored connector the original env
    path applies. Missing in both places is NOT an ARQ retry — the job
    reports an error result instead of raising.
    """
    from app import connectors, vectorstore
    from app.slack_ingest import ingest_slack as run_ingest

    logger.info("job ingest_slack: org_id=%s user_id=%s", org_id, user_id)

    org_cfg = await _load_org_connector(org_id, "slack")

    token: str | None = None
    channel_ids: list[str] | None = None
    if org_cfg is not None:
        try:
            token, channel_ids = connectors.resolve_slack_config(org_cfg)
        except ValueError as e:
            # Org config present but unusable (e.g. empty token): permanent
            # waiting-for-setup state, not an ARQ retry.
            result = {"error": str(e), "channels": 0, "chunks_stored": 0, "chunks_deleted": 0}
            await _audit_ingest_summary(org_id, user_id, {"source": "slack", "error": str(e)})
            return result

    try:
        per_channel = run_ingest(user_id=user_id, org_id=org_id, token=token, channel_ids=channel_ids)
    except ValueError as e:
        # No org connector AND nothing at the process level: also permanent.
        # (With an org config this can't happen — resolve validated above.)
        error = str(e) if token is not None else f"no slack connector configured for org {org_id}"
        result = {"error": error, "channels": 0, "chunks_stored": 0, "chunks_deleted": 0}
        await _audit_ingest_summary(org_id, user_id, {"source": "slack", "error": error})
        return result

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

    await _audit_ingest_summary(org_id, user_id, {"source": "slack", **counts})
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