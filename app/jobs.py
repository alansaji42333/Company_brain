"""Helpers for enqueueing ARQ jobs and querying their status.

The ARQ connection pool lives on `app.state.redis` (an `arq.ArqRedis`), created
in server.py's lifespan and shared across requests. These helpers accept the
pool as an argument so they don't import the FastAPI app — keeping the job
layer testable and decoupled from the server.

Job ownership is org-scoped: `org_id` (plus the requesting `user_id`) is
recorded in Redis under `job:{job_id}` so the status endpoint can only be
polled by the org that enqueued the job.
"""
import logging

from arq import ArqRedis
from arq.jobs import Job, JobStatus

logger = logging.getLogger(__name__)


async def enqueue_job(
    pool: ArqRedis,
    function: str,
    user_id: str,
    org_id: str,
    **kwargs,
) -> str:
    """Enqueue a background job and tag it with its owning org/user.

    `user_id` and `org_id` are passed to the worker function as keyword
    arguments (the job functions accept them explicitly). Returns the job_id.
    Raises RuntimeError if the queue rejected the job.
    """
    kwargs["user_id"] = user_id
    kwargs["org_id"] = org_id
    job = await pool.enqueue_job(function, _job_id=None, **kwargs)
    if job is None:
        raise RuntimeError(f"Failed to enqueue job '{function}' (already enqueued or queue full?)")
    await pool.hset(
        f"job:{job.job_id}",
        mapping={"user_id": user_id, "org_id": org_id, "status": "queued"},
    )
    logger.info("enqueued %s job_id=%s org_id=%s user_id=%s", function, job.job_id, org_id, user_id)
    return job.job_id


async def enqueue_ingest_drive(
    pool: ArqRedis, user_id: str, org_id: str, folder_id: str | None = None
) -> str:
    return await enqueue_job(pool, "ingest_drive", user_id, org_id, folder_id=folder_id)


async def enqueue_ingest_slack(pool: ArqRedis, user_id: str, org_id: str) -> str:
    return await enqueue_job(pool, "ingest_slack", user_id, org_id)


async def enqueue_synthesis(pool: ArqRedis, user_id: str, org_id: str) -> str:
    return await enqueue_job(pool, "synthesize", user_id, org_id)


def _decode(value) -> str | None:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else value


async def get_job_status(pool: ArqRedis, job_id: str, user_id: str, org_id: str) -> dict | None:
    """Return the status of a job owned by (org_id, user_id), or None."""
    owner_org = _decode(await pool.hget(f"job:{job_id}", "org_id"))
    owner_user = _decode(await pool.hget(f"job:{job_id}", "user_id"))
    if owner_org is None or owner_user is None:
        return None
    if owner_org != org_id or owner_user != user_id:
        return None

    job = Job(job_id, redis=pool)
    status = await job.status()
    result: dict | None = None
    if status == JobStatus.complete:
        try:
            result = await job.result()
        except Exception as e:
            result = {"error": str(e)}
    elif status == JobStatus.not_found:
        return None

    return {
        "job_id": job_id,
        "org_id": org_id,
        "status": status.name.lower(),
        "result": result,
    }
