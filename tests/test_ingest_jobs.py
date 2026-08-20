"""Integration tests for the async ingest API (/api/ingest/*).

Mocks the ARQ Redis pool (no real Redis required). Verifies that the drive
and slack endpoints enqueue the right function and return a job_id, and that
the status endpoint returns job state and enforces per-user ownership.
"""
import pytest
from jose import jwt
from fastapi.testclient import TestClient


def _make_token(secret: str, user_id: str = "user1") -> str:
    return jwt.encode({"sub": user_id}, secret, algorithm="HS256")


class _FakeJob:
    def __init__(self, job_id: str):
        self.job_id = job_id


class _FakePool:
    """Minimal stub of arq.ArqRedis covering enqueue + hset/hget + Job lookup."""
    def __init__(self):
        self.enqueued = []  # list of (function, kwargs)
        self.ownership = {}  # job_id -> user_id
        self.results = {}    # job_id -> {status, result}

    async def enqueue_job(self, function, _job_id=None, **kwargs):
        job_id = f"job-{len(self.enqueued) + 1}"
        self.enqueued.append((function, kwargs))
        self.ownership[job_id] = kwargs.get("user_id", "")
        self.results[job_id] = {"status": "queued", "result": None}
        return _FakeJob(job_id)

    async def hset(self, key, mapping=None):
        # ARQ helpers tag job:{id} with user_id/status; we already record owner.
        if key.startswith("job:") and mapping and "status" in mapping:
            jid = key.split(":", 1)[1]
            if jid in self.results:
                self.results[jid]["status"] = mapping["status"]

    async def hget(self, key, field):
        if key.startswith("job:") and field == "user_id":
            jid = key.split(":", 1)[1]
            return self.ownership.get(jid)
        return None

    async def close(self):
        pass


@pytest.fixture
def client(monkeypatch, tmp_path):
    import app.config as config
    import app.auth as auth
    monkeypatch.setattr(config, "JWT_SECRET", "test-secret")
    monkeypatch.setattr(auth, "JWT_SECRET", "test-secret")
    monkeypatch.setattr(config, "AUTH0_ENABLED", False)
    monkeypatch.setattr(auth, "AUTH0_ENABLED", False)

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    import app.database as database
    test_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    test_session = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(database, "async_session", test_session)

    pool = _FakePool()
    from app.server import app
    app.state.redis = pool
    return TestClient(app), pool


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {_make_token('test-secret', 'user1')}"}


def test_ingest_drive_enqueues_and_returns_job_id(client, auth_headers):
    c, pool = client
    res = c.post("/api/ingest/drive", headers=auth_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "queued"
    assert data["job_id"].startswith("job-")
    assert len(pool.enqueued) == 1
    fn, kwargs = pool.enqueued[0]
    assert fn == "ingest_drive"
    assert kwargs["user_id"] == "user1"


def test_ingest_drive_with_folder_id(client, auth_headers):
    c, pool = client
    res = c.post("/api/ingest/drive?folder_id=FOLDER123", headers=auth_headers)
    assert res.status_code == 200
    fn, kwargs = pool.enqueued[0]
    assert fn == "ingest_drive"
    assert kwargs["folder_id"] == "FOLDER123"


def test_ingest_slack_enqueues(client, auth_headers):
    c, pool = client
    res = c.post("/api/ingest/slack", headers=auth_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "queued"
    fn, kwargs = pool.enqueued[0]
    assert fn == "ingest_slack"
    assert kwargs["user_id"] == "user1"


def test_ingest_status_returns_result_when_complete(client, auth_headers, monkeypatch):
    c, pool = client
    res = c.post("/api/ingest/drive", headers=auth_headers)
    job_id = res.json()["job_id"]

    expected = {"job_id": job_id, "status": "complete", "result": {"chunks_stored": 42}}

    async def fake_status(pool_arg, jid, user_id):
        assert jid == job_id
        assert user_id == "user1"
        return expected

    monkeypatch.setattr("app.jobs.get_job_status", fake_status)

    res2 = c.get(f"/api/ingest/status/{job_id}", headers=auth_headers)
    assert res2.status_code == 200, res2.text
    assert res2.json() == expected


def test_ingest_status_404_for_unknown_job(client, auth_headers):
    c, _ = client
    res = c.get("/api/ingest/status/nope", headers=auth_headers)
    assert res.status_code == 404


def test_ingest_status_rejects_other_user(client, auth_headers):
    c, pool = client
    res = c.post("/api/ingest/drive", headers=auth_headers)
    job_id = res.json()["job_id"]

    other_headers = {"Authorization": f"Bearer {_make_token('test-secret', 'user2')}"}
    res2 = c.get(f"/api/ingest/status/{job_id}", headers=other_headers)
    assert res2.status_code == 404  # not owned -> not found


def test_async_ingest_requires_auth(client):
    c, _ = client
    assert c.post("/api/ingest/drive").status_code == 401
    assert c.post("/api/ingest/slack").status_code == 401
    assert c.get("/api/ingest/status/job-1").status_code == 401


def test_async_ingest_503_when_no_redis(monkeypatch):
    import app.config as config
    import app.auth as auth
    monkeypatch.setattr(config, "JWT_SECRET", "test-secret")
    monkeypatch.setattr(auth, "JWT_SECRET", "test-secret")
    monkeypatch.setattr(config, "AUTH0_ENABLED", False)
    monkeypatch.setattr(auth, "AUTH0_ENABLED", False)

    from app.server import app
    app.state.redis = None
    c = TestClient(app)
    headers = {"Authorization": f"Bearer {_make_token('test-secret', 'user1')}"}
    assert c.post("/api/ingest/drive", headers=headers).status_code == 503
    assert c.post("/api/ingest/slack", headers=headers).status_code == 503
    assert c.get("/api/ingest/status/job-1", headers=headers).status_code == 503