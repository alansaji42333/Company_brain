"""Drive OAuth self-serve onboarding: start + callback endpoints.

Hermetic — no Google/HTTP network: the token exchange is faked at the
`drive_oauth.exchange_code` boundary. Covers: state round-trip, tampered /
expired / org-mismatched state, successful callback stores an encrypted
connector row + audits + redirects, missing refresh_token saves nothing,
and start-endpoint authorization (admin-only, member-of, configuration
checks).
"""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

FRONTEND = "https://frontend.example"
CONNECTORS_PAGE = f"{FRONTEND}/connectors"


@pytest_asyncio.fixture
async def oauth_env(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app import auth, config, database

    monkeypatch.setattr(config, "JWT_SECRET", "test-secret")
    monkeypatch.setattr(auth, "JWT_SECRET", "test-secret")
    monkeypatch.setattr(config, "AUTH0_ENABLED", False)
    monkeypatch.setattr(auth, "AUTH0_ENABLED", False)
    monkeypatch.setattr(config, "GOOGLE_OAUTH_CLIENT_ID", "oauth-client-id")
    monkeypatch.setattr(config, "GOOGLE_OAUTH_CLIENT_SECRET", "oauth-client-secret")
    monkeypatch.setattr(config, "GOOGLE_OAUTH_REDIRECT_URI", "https://backend.example/api/v1/orgs/{}/connectors/drive/oauth/callback")
    monkeypatch.setattr(config, "FRONTEND_URL", FRONTEND)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'oauth.db'}")
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


async def _seed_org(session_factory, owner: str, extra_member: str | None = None, extra_role: str = "member"):
    from app.orgs import add_member, create_org

    async with session_factory() as db:
        org = await create_org(db, name="Acme", slug=None, owner_user_id=owner)
        if extra_member:
            await add_member(db, org["id"], extra_member, extra_role)
        await db.commit()
        return org["id"]


class _FakeCreds:
    """Mimics google Credentials.to_json() (a JSON string) post-parse."""

    def __init__(self, with_refresh=True):
        self._dict = {
            "token": "ya29.fresh",
            "refresh_token": "1//refresh" if with_refresh else None,
            "expiry": "2030-01-01T00:00:00Z",
        }

    def to_dict(self):
        import json
        return json.loads(json.dumps(self._dict))


@pytest.fixture
def exchange_spy(monkeypatch):
    """Fakes the Google token exchange, recording calls."""
    from app import drive_oauth

    calls = {"codes": [], "result": None, "raise": None}

    def fake_exchange(code: str, credentials_web_dict: dict) -> dict:
        calls["codes"].append(code)
        if calls["raise"]:
            raise calls["raise"]
        assert credentials_web_dict.get("client_id") == "oauth-client-id"
        return calls["result"] if calls["result"] is not None else _FakeCreds().to_dict()

    monkeypatch.setattr(drive_oauth, "exchange_code", fake_exchange)
    return calls


# --- start endpoint -----------------------------------------------------------

def test_start_redirects_to_google_consent(oauth_env):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    res = client.get(
        f"/api/v1/orgs/{org}/connectors/drive/oauth/start",
        params={"folder_id": "F-123"},
        headers={"Authorization": f"Bearer {_token('admin1', org)}"},
        follow_redirects=False,
    )
    assert res.status_code == 302, res.text
    location = res.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    for fragment in (
        "response_type=code",
        "client_id=oauth-client-id",
        "access_type=offline",
        "prompt=consent",  # refresh_token is mandatory
    ):
        assert fragment in location, location
    # The state travels with the redirect and round-trips against the path org.
    state = next(p.split("=", 1)[1] for p in location.split("&") if p.startswith("state="))
    from app import drive_oauth
    claims = drive_oauth.verify_state(state, org)
    assert claims["user_id"] == "admin1" and claims["folder_id"] == "F-123"


def test_start_requires_admin_role(oauth_env):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1", extra_member="peon", extra_role="member"))
    res = client.get(
        f"/api/v1/orgs/{org}/connectors/drive/oauth/start",
        params={"folder_id": "F-123"},
        headers={"Authorization": f"Bearer {_token('peon', org)}"},
        follow_redirects=False,
    )
    assert res.status_code == 403


def test_start_requires_membership(oauth_env):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    res = client.get(
        f"/api/v1/orgs/{org}/connectors/drive/oauth/start",
        params={"folder_id": "F-123"},
        headers={"Authorization": f"Bearer {_token('boss_of_other_org', 'org_other_member')}"},
        follow_redirects=False,
    )
    assert res.status_code == 403


def test_start_requires_folder_id(oauth_env):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    res = client.get(
        f"/api/v1/orgs/{org}/connectors/drive/oauth/start",
        headers={"Authorization": f"Bearer {_token('admin1', org)}"},
        follow_redirects=False,
    )
    assert res.status_code == 400


def test_start_503_when_oauth_unconfigured(oauth_env, monkeypatch):
    from app import config

    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    monkeypatch.setattr(config, "GOOGLE_OAUTH_CLIENT_ID", "")
    res = client.get(
        f"/api/v1/orgs/{org}/connectors/drive/oauth/start",
        params={"folder_id": "F-123"},
        headers={"Authorization": f"Bearer {_token('admin1', org)}"},
        follow_redirects=False,
    )
    assert res.status_code == 503
    assert "GOOGLE_OAUTH_CLIENT_ID" in res.json()["detail"]


def test_start_503_when_no_signing_secret(oauth_env, monkeypatch):
    from app import config

    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    monkeypatch.setattr(config, "JWT_SECRET", "")
    res = client.get(
        f"/api/v1/orgs/{org}/connectors/drive/oauth/start",
        params={"folder_id": "F-123"},
        headers={"Authorization": f"Bearer {_token('admin1', org)}"},
        follow_redirects=False,
    )
    assert res.status_code == 503


# --- callback -----------------------------------------------------------------

def _callback(client, org: str, state: str, code: str = "4/valid-auth-code", follow: bool = False):
    """Call the callback unfollowed by default so the 302 target is asserted
    directly."""
    return client.get(
        f"/api/v1/orgs/{org}/connectors/drive/oauth/callback",
        params={"code": code, "state": state},
        follow_redirects=follow,
    )


def _state_for(org: str, user_id: str = "admin1", folder_id: str = "F-123") -> str:
    from app import drive_oauth
    return drive_oauth.make_state(org, user_id, folder_id)


async def _connector_rows(session_factory, org: str):
    from app.database import OrgConnectorConfig

    async with session_factory() as db:
        rows = (await db.execute(select(OrgConnectorConfig))).scalars().all()
        return [r for r in rows if r.org_id == org]


def test_callback_success_stores_encrypted_row_and_redirects(oauth_env, exchange_spy):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    state = _state_for(org)

    res = _callback(client, org, state, follow=False)
    assert res.status_code == 302
    assert res.headers["location"] == f"{CONNECTORS_PAGE}?drive=connected"
    assert exchange_spy["codes"] == ["4/valid-auth-code"]

    # The stored row decrypts to the exact config the consumer expects.
    from app import connectors

    rows = asyncio.run(_connector_rows(session_factory, org))
    assert len(rows) == 1
    ciphertext = rows[0].config_encrypted
    assert "refresh_token" not in ciphertext  # sealed, no plaintext secrets

    async def _get():
        async with session_factory() as db:
            return await connectors.get_connector(db, org, "drive")

    config = asyncio.run(_get())
    assert config["folder_id"] == "F-123"
    assert config["token_json"]["refresh_token"] == "1//refresh"
    assert config["credentials_json"]["client_id"] == "oauth-client-id"

    # Audit: ACTION_CONNECTOR_SET attributed to the state's user (not whoever
    # clicks), payload safe.
    from app import audit
    from app.database import AuditLog

    async def _audit_rows():
        async with session_factory() as db:
            return (await db.execute(select(AuditLog))).scalars().all()

    logs = asyncio.run(_audit_rows())
    set_rows = [r for r in logs if r.action_type == audit.ACTION_CONNECTOR_SET]
    assert len(set_rows) == 1
    assert set_rows[0].user_id == "admin1"


def test_callback_tampered_state_rejected(oauth_env, exchange_spy):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))

    # A different org's valid state is still tamper evidence for THIS org.
    other_org = asyncio.run(_seed_org(session_factory, "admin2"))
    foreign_state = _state_for(other_org, user_id="admin2")
    res = _callback(client, org, foreign_state, follow=False)
    assert res.status_code == 302
    assert res.headers["location"] == f"{CONNECTORS_PAGE}?drive=error&reason=org_mismatch"
    assert exchange_spy["codes"] == [], "code must never be exchanged with untrusted state"
    assert asyncio.run(_connector_rows(session_factory, org)) == []

    # A literally corrupted state string.
    corrupted = _state_for(org)[:-3] + "abc"
    res = _callback(client, org, corrupted, follow=False)
    assert "drive=error&reason=invalid_state" in res.headers["location"]
    assert exchange_spy["codes"] == []


def test_callback_expired_state_rejected(oauth_env, exchange_spy, monkeypatch):
    from app import drive_oauth

    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    monkeypatch.setattr(drive_oauth, "STATE_TTL_SECONDS", -1)  # already stale at issue time
    state = _state_for(org)
    monkeypatch.setattr(drive_oauth, "STATE_TTL_SECONDS", 600)

    res = _callback(client, org, state, follow=False)
    assert "drive=error&reason=expired_state" in res.headers["location"]
    assert exchange_spy["codes"] == []
    assert asyncio.run(_connector_rows(session_factory, org)) == []


def test_callback_missing_refresh_token_saves_nothing(oauth_env, exchange_spy):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    state = _state_for(org)
    exchange_spy["result"] = _FakeCreds(with_refresh=False).to_dict()

    res = _callback(client, org, state, follow=False)
    assert res.status_code == 302
    assert res.headers["location"] == f"{CONNECTORS_PAGE}?drive=error&reason=no_refresh_token"
    # Nothing was written: without a refresh_token the connection would die
    # at the first expiry — the admin must re-consent (prompt=consent forces
    # Google to re-issue one).
    assert asyncio.run(_connector_rows(session_factory, org)) == []
    from app import audit
    from app.database import AuditLog

    async def _audit_rows():
        async with session_factory() as db:
            return list((await db.execute(select(AuditLog))).scalars().all())

    assert not [r for r in asyncio.run(_audit_rows()) if r.action_type == audit.ACTION_CONNECTOR_SET]


def test_callback_exchange_failure_redirects_error(oauth_env, exchange_spy):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    exchange_spy["raise"] = RuntimeError("grant_invalid")
    state = _state_for(org)

    res = _callback(client, org, state, follow=False)
    assert "drive=error&reason=exchange_failed" in res.headers["location"]
    assert asyncio.run(_connector_rows(session_factory, org)) == []


def test_callback_requires_params(oauth_env, exchange_spy):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    res = client.get(
        f"/api/v1/orgs/{org}/connectors/drive/oauth/callback",
        follow_redirects=False,
    )
    assert res.status_code == 302
    assert "drive=error&reason=missing_params" in res.headers["location"]


def test_callback_still_503_when_oauth_unconfigured(oauth_env, exchange_spy, monkeypatch):
    from app import config

    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    monkeypatch.setattr(config, "GOOGLE_OAUTH_REDIRECT_URI", "")
    res = _callback(client, org, _state_for(org), follow=False)
    assert res.status_code == 503


# --- state unit contract --------------------------------------------------------

def test_state_roundtrip_and_mismatch():
    from app import drive_oauth

    state = _state_for("org_aaa", user_id="u1", folder_id="F1")
    claims = drive_oauth.verify_state(state, "org_aaa")
    assert claims == {
        "purpose": "drive_oauth",
        "org_id": "org_aaa",
        "user_id": "u1",
        "folder_id": "F1",
        "iat": claims["iat"],
        "exp": claims["exp"],
    }
    assert claims["exp"] - claims["iat"] == drive_oauth.STATE_TTL_SECONDS

    with pytest.raises(drive_oauth.OAuthStateError, match="org_mismatch"):
        drive_oauth.verify_state(state, "org_bbb")


def test_state_refuses_without_signing_secret(monkeypatch):
    from app import config, drive_oauth

    monkeypatch.setattr(config, "JWT_SECRET", "")
    with pytest.raises(RuntimeError, match="signing secret"):
        drive_oauth.make_state("org_aaa", "u1", "F1")
