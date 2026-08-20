"""ARQ worker configuration.

Registers the ingestion and synthesis coroutines as background jobs and
exposes the WorkerSettings used by `arq app.worker.WorkerSettings`.

The job functions here are thin async wrappers around the existing sync
ingestion pipelines (drive_ingest, slack_ingest, skill_synthesis). They run
inside the worker process, off the request thread, so HTTP endpoints return
immediately with a job_id.
"""
import logging
from arq.connections import RedisSettings

from app.config import REDIS_URL

logger = logging.getLogger(__name__)


def redis_settings() -> RedisSettings:
    """Build ARQ RedisSettings from the configured REDIS_URL."""
    return RedisSettings.from_dsn(REDIS_URL)


# --- registered job functions ------------------------------------------
# Each must accept the ARQ context (`ctx`) as the first argument.

async def ingest_drive(ctx: dict, folder_id: str | None = None, user_id: str = "") -> dict:
    """Ingest a Google Drive folder: extract → chunk → embed → store."""
    from app.drive_ingest import ingest_drive_folder
    from app.chunking import chunk_documents
    from app.vectorstore import add_chunks

    logger.info("job ingest_drive: folder_id=%s user_id=%s", folder_id, user_id)
    documents = ingest_drive_folder(folder_id, user_id=user_id)
    chunks = chunk_documents(documents, user_id=user_id)
    add_chunks(chunks)
    logger.info("job ingest_drive done: %d files, %d chunks", len(documents), len(chunks))
    return {"files_processed": len(documents), "chunks_stored": len(chunks)}


async def ingest_slack(ctx: dict, user_id: str = "") -> dict:
    """Ingest Slack channels: history → threads → chunk → embed → store."""
    from app.slack_ingest import ingest_slack as run_ingest
    from app.vectorstore import add_chunks

    logger.info("job ingest_slack: user_id=%s", user_id)
    chunks = run_ingest(user_id=user_id)
    add_chunks(chunks)
    logger.info("job ingest_slack done: %d chunks", len(chunks))
    return {"chunks_stored": len(chunks)}


async def synthesize(ctx: dict, user_id: str = "") -> dict:
    """Run skill synthesis over newly ingested chunks."""
    from app.skill_synthesis import synthesize as run_synthesis

    logger.info("job synthesize: user_id=%s", user_id)
    summary = run_synthesis(user_id=user_id)
    logger.info("job synthesize done: %s", summary)
    return summary


# --- worker entrypoint --------------------------------------------------

class WorkerSettings:
    """Configuration consumed by `arq app.worker.WorkerSettings`."""
    functions = [ingest_drive, ingest_slack, synthesize]
    redis_settings = redis_settings()
    max_jobs = 10
    job_timeout = 600
    max_tries = 3