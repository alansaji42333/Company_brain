"""Scheduled ingestion and synthesis using APScheduler.

Runs in-process alongside the web server. Cron expressions come from config.
Each scheduled run enqueues a background job on the ARQ pool (shared with the
HTTP-driven async ingest endpoints) so there is a single execution path for
ingestion and synthesis, whether triggered by a cron tick or an API call.

Scheduled jobs use SCHEDULE_USER_ID as the tenant so scheduled data is
isolated from interactive users.
"""
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from app.config import (
    SCHEDULE_ENABLED, SCHEDULE_INGEST_CRON, SCHEDULE_SYNTHESIS_CRON,
    SCHEDULE_USER_ID,
)

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
        lambda: _enqueue_safe("ingest_drive", SCHEDULE_USER_ID, folder_id=None, user_id=SCHEDULE_USER_ID),
        CronTrigger.from_crontab(SCHEDULE_INGEST_CRON),
        id="drive_ingest",
        replace_existing=True,
    )
    _scheduler.add_job(
        lambda: _enqueue_safe("ingest_slack", SCHEDULE_USER_ID, user_id=SCHEDULE_USER_ID),
        CronTrigger.from_crontab(SCHEDULE_INGEST_CRON),
        id="slack_ingest",
        replace_existing=True,
    )
    _scheduler.add_job(
        lambda: _enqueue_safe("synthesize", SCHEDULE_USER_ID, user_id=SCHEDULE_USER_ID),
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