"""Scheduled ingestion and synthesis using APScheduler.

Runs in-process alongside the web server. Cron expressions come from config.
Each scheduled run enqueues a background job on the ARQ pool (shared with the
HTTP-driven async ingest endpoints) so there is a single execution path for
ingestion and synthesis, whether triggered by a cron tick or an API call.

Every scheduled tick iterates over **all organizations** in the database and
enqueues one job per org, so each tenant's data is ingested and synthesized
independently. SCHEDULE_USER_ID is only the acting user recorded for
provenance on those jobs.
"""
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from app.config import (
    SCHEDULE_ENABLED, SCHEDULE_INGEST_CRON, SCHEDULE_SYNTHESIS_CRON,
    SCHEDULE_USER_ID,
)
from app.database import SessionLocal
from app.orgs import list_all_org_ids

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
# Set by server.start_scheduler(app) — the ARQ pool from app.state.redis.
_pool = None


def set_pool(pool):
    global _pool
    _pool = pool


async def _enqueue_safe(function: str, user_id: str, **kwargs):
    if _pool is None:
        logger.warning("Scheduler cannot enqueue %s: ARQ pool not available", function)
        return
    try:
        await _pool.enqueue_job(function, _job_id=None, user_id=user_id, **kwargs)
        logger.info("Scheduler enqueued %s for user_id=%s", function, user_id)
    except Exception:
        logger.exception("Scheduler failed to enqueue %s", function)


async def _enqueue_for_all_orgs(function: str, **kwargs):
    """Enqueue `function` once per organization in the database."""
    if _pool is None:
        logger.warning("Scheduler cannot enqueue %s: ARQ pool not available", function)
        return
    try:
        async with SessionLocal() as db:
            org_ids = await list_all_org_ids(db)
    except Exception:
        logger.exception("Scheduler could not list organizations for %s", function)
        return

    if not org_ids:
        logger.info("Scheduler skipped %s: no organizations configured", function)
        return

    for org_id in org_ids:
        await _enqueue_safe(function, SCHEDULE_USER_ID, org_id=org_id, **kwargs)
    logger.info("Scheduler enqueued %s for %d org(s)", function, len(org_ids))


def start_scheduler(app=None):
    global _scheduler
    if not SCHEDULE_ENABLED:
        logger.info("Scheduler disabled (SCHEDULE_ENABLED=false)")
        return
    if _scheduler is not None:
        return
    if app is not None:
        set_pool(getattr(app.state, "redis", None))

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        lambda: _enqueue_for_all_orgs("ingest_drive", folder_id=None),
        CronTrigger.from_crontab(SCHEDULE_INGEST_CRON),
        id="drive_ingest",
        replace_existing=True,
    )
    _scheduler.add_job(
        lambda: _enqueue_for_all_orgs("ingest_slack"),
        CronTrigger.from_crontab(SCHEDULE_INGEST_CRON),
        id="slack_ingest",
        replace_existing=True,
    )
    _scheduler.add_job(
        lambda: _enqueue_for_all_orgs("synthesize"),
        CronTrigger.from_crontab(SCHEDULE_SYNTHESIS_CRON),
        id="synthesis",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Scheduler started (ingest=%s synthesis=%s)", SCHEDULE_INGEST_CRON, SCHEDULE_SYNTHESIS_CRON)


def stop_scheduler():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
