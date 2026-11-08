"""Regression tests for the scheduler's registered jobs.

Bug D: the three add_job calls passed a sync lambda returning a coroutine —
AsyncIOScheduler never awaits that, so scheduled ingest/synthesis silently
never ran. The registered callables MUST be coroutine functions, and invoking
them must actually reach the ARQ enqueue path (once per org).
"""
import asyncio
import inspect
from typing import ClassVar

import pytest

from app.config import SCHEDULE_USER_ID

ORGS = ["org_aaa", "org_bbb"]


class _FakeScheduler:
    """Records every instance start_scheduler creates (deterministic capture)."""

    instances: ClassVar[list] = []

    def __init__(self):
        self.jobs = {}  # job id -> func
        _FakeScheduler.instances.append(self)

    def add_job(self, func, trigger, id=None, replace_existing=False):
        self.jobs[id] = func

    def start(self):
        pass

    def shutdown(self, wait=False):
        pass


class _FakePool:
    def __init__(self):
        self.enqueued = []  # (function, kwargs)

    async def enqueue_job(self, function, _job_id=None, **kwargs):
        self.enqueued.append((function, kwargs))


class _FakeSession:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def scheduler_env(monkeypatch):
    from app import scheduler

    monkeypatch.setattr(scheduler, "_scheduler", None)
    pool = _FakePool()
    scheduler.set_pool(pool)

    async def fake_list_orgs(db):
        return ORGS

    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(scheduler, "list_all_org_ids", fake_list_orgs)
    return scheduler, pool


def test_registered_jobs_are_coroutine_functions(monkeypatch):
    from app import scheduler

    _FakeScheduler.instances.clear()
    monkeypatch.setattr(scheduler, "AsyncIOScheduler", _FakeScheduler)
    monkeypatch.setattr(scheduler, "SCHEDULE_ENABLED", True)
    monkeypatch.setattr(scheduler, "_scheduler", None)
    scheduler.start_scheduler(None)

    try:
        assert len(_FakeScheduler.instances) == 1
        fake = _FakeScheduler.instances[-1]
        assert set(fake.jobs) == {"drive_ingest", "slack_ingest", "synthesis"}
        for job_id, func in fake.jobs.items():
            assert inspect.iscoroutinefunction(func), (
                f"job {job_id} is not a coroutine function — AsyncIOScheduler "
                "would silently never await it (sync-lambda regression)"
            )
    finally:
        scheduler.stop_scheduler()


def test_registered_jobs_enqueue_once_per_org(scheduler_env):
    scheduler, pool = scheduler_env

    funcs = (
        (scheduler._scheduled_drive_ingest, "ingest_drive"),
        (scheduler._scheduled_slack_ingest, "ingest_slack"),
        (scheduler._scheduled_synthesis, "synthesize"),
    )
    for func, arq_name in funcs:
        assert inspect.iscoroutinefunction(func)
        asyncio.run(func())

        for fn_name, kwargs in pool.enqueued:
            assert fn_name == arq_name, "each scheduled job must map to its own ARQ function"
            assert kwargs["user_id"] == SCHEDULE_USER_ID

        orgs_seen = [kwargs["org_id"] for _, kwargs in pool.enqueued]
        assert orgs_seen == ORGS, f"{arq_name} must be enqueued exactly once per org"
        pool.enqueued.clear()
