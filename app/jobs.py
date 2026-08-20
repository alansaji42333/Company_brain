"""Helpers for enqueueing ARQ jobs and querying their status.

The ARQ connection pool lives on `app.state.redis` (an `arq.ArqRedis`), created
in server.py's lifespan and shared across requests. These helpers accept the
pool as an argument so they don't import the FastAPI app — keeping the job
layer testable and decoupled from the server.

Job ownership (user_id) is recorded in Redis under `job:{job_id}` so the status
endpoint can enforce that a user only polls their own jobs.
"""
import logging
from arq import ArqRedis
from arq.jobs import Job, JobStatus

logger = logging.getLogger(__name__)


async def enqueue_job(
    pool: ArqRedis,
    function: str,
    user_id: str,
    **kwargs,
) -> str:
    """Enqueue a background job and tag it with the owning user_id.

    `user_id` is passed to the worker function as a keyword argument (the
    job functions accept it explicitly). Returns the job_id. Raises
    RuntimeError if the queue rejected the job.
    """
    kwargs["user_id"] = user_id
    job = await pool.enqueue_job(function, _job_id=None, **kwargs)
    if job is None:
        raise RuntimeError(f"Failed to enqueue job '{function}' (already enqueued or queue full?)")
    await pool.hset(f"job:{job.job_id}", mapping={"user_id": user_id, "status": "queued"})
    logger.info("enqueued %s job_id=%s user_id=%s", function, job.job_id, user_id)
    return job.job_id


async def enqueue_ingest_drive(
    pool: ArqRedis, user_id: str, folder_id: str | None = None
) -> str:
    return await enqueue_job(pool, "ingest_drive", user_id, folder_id=folder_id)


async def enqueue_ingest_slack(pool: ArqRedis, user_id: str) -> str:
    return await enqueue_job(pool, "ingest_slack", user_id)


async def enqueue_synthesis(pool: ArqRedis, user_id: str) -> str:
    return await enqueue_job(pool, "synthesize", user_id)


async def get_job_status(pool: ArqRedis, job_id: str, user_id: str) -> dict | None:
    """Return the status of a job owned by `user_id`, or None if not found/owned."""
    owner = await pool.hget(f"job:{job_id}", "user_id")
    if owner is None:
        return None
    if (owner.decode() if isinstance(owner, bytes) else owner) != user_id:
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
        "status": status.name.lower(),
        "result": result,
    }