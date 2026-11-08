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


# Load-failure kinds returned by _load_org_connector (curated strings — safe
# for logs, audit payloads and API-visible job results; never raw exception
# text, config, or tokens).
_LOAD_STORE_UNAVAILABLE = "connector store unavailable"
_LOAD_CONFIG_CORRUPT = "stored connector config is corrupt or undecryptable"


async def _load_org_connector(org_id: str, source: str) -> tuple[dict | None, str | None]:
    """Load and decrypt the org's stored connector config for `source`.

    Returns ``(org_cfg, failure)``:

    - ``(config, None)`` — a present, decryptable stored config;
    - ``(None, None)`` — the org genuinely has NO stored row;
    - ``(None, failure)`` — the store could not be trusted:

      * ``connector store unavailable`` — DB down or the table missing
        (e.g. migration not yet applied). Only the explicitly bound legacy
        org may fall back to env/file credentials, and ONLY through the
        centralized resolver gate in app.connectors (which re-checks the
        binding and env presence).
      * ``stored connector config is corrupt or undecryptable`` — a
        present-but-unreadable config must fail closed for EVERY org; it
        must never masquerade as an absent row, which could authorize the
        global-credential fallback.

    Runs inside its own short-lived session which is closed before any of the
    (sync, potentially long) ingest work starts. Callers MUST treat a failure
    as "stop before authentication, fetching, embedding, storage, or deletion".
    """
    from app import connectors
    from app.database import async_session

    try:
        async with async_session() as db:
            return await connectors.get_connector(db, org_id, source), None
    except ValueError:
        # app.connectors signals an undecryptable stored config this way.
        # Fail closed for every org — this is NOT "no stored row".
        return None, _LOAD_CONFIG_CORRUPT
    except Exception:
        # No raw exception text here: the returned/logged kind is curated so a
        # DB error can never smuggle secrets anywhere. Fall back to env
        # credentials is permitted ONLY for the explicitly bound legacy org
        # (the caller enforces this via the resolver gate).
        logger.error(
            "Connector store load failed for org %s source %s (%s); only an "
            "explicitly bound legacy org may fall back to env credentials",
            org_id, source, _LOAD_STORE_UNAVAILABLE,
        )
        return None, _LOAD_STORE_UNAVAILABLE


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
    the process env/file path ONLY if this org is the explicitly bound
    LEGACY_CONNECTOR_ORG_ID (enforced in app.connectors' resolver, checked
    BEFORE any env inspection — a folder override does not grant authority).
    A missing connector is NOT an ARQ retry — it is a waiting-for-setup
    state, so the job reports an error result instead of raising. A store
    load failure or a corrupt stored config fails closed for every org.
    """
    from app import connectors, vectorstore
    from app import google_auth as google_auth_boundary
    from app.chunking import chunk_document, content_hash
    from app.drive_ingest import ingest_drive_folder

    logger.info("job ingest_drive: folder_id=%s org_id=%s user_id=%s", folder_id, org_id, user_id)

    # ALWAYS resolve (even with no stored row): the resolver is the single
    # gate deciding whether the legacy env/file fallback is authorized.
    org_cfg, load_failure = await _load_org_connector(org_id, "drive")
    if load_failure is not None:
        if load_failure == _LOAD_CONFIG_CORRUPT:
            # A corrupt stored config must fail closed — never masquerade as
            # an absent row (which could authorize global credentials).
            error = f"stored drive connector config for org {org_id} is corrupt or undecryptable; re-save the connector"
            result = {"error": error, "files_processed": 0, "chunks_stored": 0}
            await _audit_ingest_summary(org_id, user_id, {"source": "drive", "error": error})
            return result
        if not connectors.is_legacy_connector_org(org_id):
            error = f"no drive connector configured for org {org_id}"
            result = {"error": error, "files_processed": 0, "chunks_stored": 0}
            await _audit_ingest_summary(org_id, user_id, {"source": "drive", "error": error})
            return result
        # Explicitly bound legacy org: permit the env/file attempt — but only
        # through the resolver, which re-checks the binding and env presence.
        org_cfg = None

    try:
        resolved_folder, credentials_json, token_json = connectors.resolve_drive_config(org_id, org_cfg, folder_id)
    except ValueError as e:
        # No authorized credentials anywhere (or an unusable stored config):
        # a permanent waiting-for-setup state, not an ARQ retry.
        error = str(e)
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
    the only channels ingested. Without a stored connector the env path is
    available ONLY to the explicitly bound LEGACY_CONNECTOR_ORG_ID (enforced
    in app.connectors' resolver, checked BEFORE any env inspection). The
    resolved token/channel_ids are ALWAYS passed downstream explicitly —
    there is no unguarded None route into low-level env selection. A missing
    connector is NOT an ARQ retry — the job reports an error result instead
    of raising. A store load failure or a corrupt stored config fails closed
    for every org.
    """
    from app import connectors, vectorstore
    from app.slack_ingest import ingest_slack as run_ingest

    logger.info("job ingest_slack: org_id=%s user_id=%s", org_id, user_id)

    # ALWAYS resolve (even with no stored row): the resolver is the single
    # gate deciding whether the legacy env fallback is authorized.
    org_cfg, load_failure = await _load_org_connector(org_id, "slack")
    if load_failure is not None:
        if load_failure == _LOAD_CONFIG_CORRUPT:
            error = f"stored slack connector config for org {org_id} is corrupt or undecryptable; re-save the connector"
            result = {"error": error, "channels": 0, "chunks_stored": 0, "chunks_deleted": 0}
            await _audit_ingest_summary(org_id, user_id, {"source": "slack", "error": error})
            return result
        if not connectors.is_legacy_connector_org(org_id):
            error = f"no slack connector configured for org {org_id}"
            result = {"error": error, "channels": 0, "chunks_stored": 0, "chunks_deleted": 0}
            await _audit_ingest_summary(org_id, user_id, {"source": "slack", "error": error})
            return result
        # Explicitly bound legacy org: permit the env attempt — but only
        # through the resolver, which re-checks the binding and env presence.
        org_cfg = None

    try:
        token, channel_ids = connectors.resolve_slack_config(org_id, org_cfg)
    except ValueError as e:
        # No authorized credentials anywhere (or an unusable stored config):
        # a permanent waiting-for-setup state, not an ARQ retry.
        result = {"error": str(e), "channels": 0, "chunks_stored": 0, "chunks_deleted": 0}
        await _audit_ingest_summary(org_id, user_id, {"source": "slack", "error": str(e)})
        return result

    try:
        per_channel = run_ingest(user_id=user_id, org_id=org_id, token=token, channel_ids=channel_ids)
    except ValueError as e:
        # Defensive only — the resolver above already validated token and
        # channels, so run_ingest's env-selection errors cannot trigger here.
        result = {"error": str(e), "channels": 0, "chunks_stored": 0, "chunks_deleted": 0}
        await _audit_ingest_summary(org_id, user_id, {"source": "slack", "error": str(e)})
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