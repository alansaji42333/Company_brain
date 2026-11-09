"""GET /api/v1/orgs/me + GET /api/v1/orgs (org switcher) contracts.

Session 4 (real login): a brand-new Auth0 user must get a personal org
automatically, /orgs/me must return user_id/org_id/org name/role, and the
org switcher lists the caller's memberships. Switching via `X-Org-Id` is a
hint only — non-membership is a 403.
"""
import asyncio

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def api(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app import auth, config, database

    monkeypatch.setattr(config, "JWT_SECRET", "test-secret")
    monkeypatch.setattr(auth, "JWT_SECRET", "test-secret")
    monkeypatch.setattr(config, "AUTH0_ENABLED", False)
    monkeypatch.setattr(auth, "AUTH0_ENABLED", False)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'orgs_me.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "async_session", session_factory)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)

    await _create()

    from app.server import app
    app.state.redis = None
    client = TestClient(app)
    try:
        yield client, session_factory
    finally:
        await engine.dispose()


def _token(user_id: str, org_id: str | None = None) -> str:
    from jose import jwt
    payload = {"sub": user_id}
    if org_id:
        payload["org_id"] = org_id
    return jwt.encode(payload, "test-secret", algorithm="HS256")


async def _add_org_with_member(session_factory, name: str, member: str, role: str = "member"):
    from app.orgs import add_member, create_org

    async with session_factory() as db:
        org = await create_org(db, name=name, slug=None, owner_user_id="founder1")
        if member != "founder1":
            await add_member(db, org["id"], member, role)
        await db.commit()
        return {"id": org["id"], "name": name}


def test_orgs_me_returns_user_org_name_role(api):
    client, session_factory = api
    org = asyncio.run(_add_org_with_member(session_factory, "Acme Inc", "user_1", "reviewer"))
    headers = {"Authorization": f"Bearer {_token('user_1', org['id'])}"}

    res = client.get("/api/v1/orgs/me", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["user_id"] == "user_1"
    assert body["id"] == org["id"]
    assert body["name"] == "Acme Inc"
    assert body["role"] == "reviewer"
    assert any(m["user_id"] == "user_1" for m in body["members"])


def test_brand_new_user_gets_personal_org(api):
    """First login with no org hint: a personal org is created lazily and
    the user is its admin — no manual setup needed."""
    client, _session_factory = api
    headers = {"Authorization": f"Bearer {_token('brand_new_user')}"}  # no org claim

    res = client.get("/api/v1/orgs/me", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["user_id"] == "brand_new_user"
    assert body["role"] == "admin"
    assert body["slug"].startswith("personal-")
    assert "brand_new_user" in body["name"]

    # It is a real, persisted membership — visible in the switcher list.
    res = client.get("/api/v1/orgs", headers=headers)
    assert res.status_code == 200
    orgs = res.json()["orgs"]
    assert len(orgs) == 1 and orgs[0]["id"] == body["id"]


def test_org_switcher_lists_all_memberships(api):
    client, session_factory = api
    org_a = asyncio.run(_add_org_with_member(session_factory, "Org A", "multi"))
    org_b = asyncio.run(_add_org_with_member(session_factory, "Org B", "multi"))

    headers = {"Authorization": f"Bearer {_token('multi', org_a['id'])}"}
    res = client.get("/api/v1/orgs", headers=headers)
    assert res.status_code == 200, res.text
    orgs = {(o["id"], o["name"], o["role"]) for o in res.json()["orgs"]}
    assert orgs == {(org_a["id"], "Org A", "member"), (org_b["id"], "Org B", "member")}

    # X-Org-Id switching works because membership exists server-side. Real
    # Auth0 tokens carry no org_id claim, so the header is the only hint
    # (a token WITH an org claim takes precedence by design).
    no_claim = f"Bearer {_token('multi')}"
    res = client.get("/api/v1/orgs/me", headers={"Authorization": no_claim, "X-Org-Id": org_b["id"]})
    assert res.status_code == 200
    assert res.json()["id"] == org_b["id"]

    # ...but an org the user is NOT a member of is a 403, never a leak.
    res = client.get("/api/v1/orgs/me", headers={"Authorization": no_claim, "X-Org-Id": "org_nope"})
    assert res.status_code == 403


def test_org_requires_authentication(api):
    client, _ = api
    assert client.get("/api/v1/orgs/me").status_code == 401
    assert client.get("/api/v1/orgs").status_code == 401


def test_invalid_token_rejected(api):
    client, _ = api
    res = client.get("/api/v1/orgs/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert res.status_code == 401
