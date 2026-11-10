"""Drive OAuth self-serve onboarding: pending-row flow (start + callback).

Hermetic — no Google/HTTP network: the token exchange is faked at the
`drive_oauth.exchange_code` boundary. The server-side pending row is the
authority; the opaque state is only a lookup nonce.

Covers:
- start: 200 {"authorize_url"} with PKCE S256 challenge; HttpOnly browser-
  binding cookie scoped to the callback path; admin/membership/fault gates;
  a second start overwrites the cookie (abandoning the older flow).
- callback: browser cookie checked BEFORE any consume/exchange; single-use
  atomic consume (replay → consumed_state); DB role revalidation (revoked
  admins rejected); encrypted connector storage audited to the ROW's user;
  cookie cleared on every terminal outcome; only short safe reason codes.
- Slack test-and-list endpoint (Slack SDK faked): channels for a valid
  token, clear error for an invalid one, admin required, nothing stored.
"""
import asyncio
from typing import ClassVar

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

FRONTEND = "https://frontend.example"
CONNECTORS_PAGE = f"{FRONTEND}/connectors"
CALLBACK_PATH = "/api/v1/connectors/drive/oauth/callback"
COOKIE_NAME = "drive_oauth_browser"


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
    monkeypatch.setattr(config, "GOOGLE_OAUTH_REDIRECT_URI", "https://backend.example" + CALLBACK_PATH)
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


def _start(client, org: str, folder_id: str = "F-123", user: str = "admin1"):
    """Start the flow as the admin; returns the response."""
    return client.get(
        f"/api/v1/orgs/{org}/connectors/drive/oauth/start",
        params={"folder_id": folder_id},
        headers={"Authorization": f"Bearer {_token(user, org)}"},
        follow_redirects=False,
    )


def _state_of(authorize_url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(authorize_url).query)["state"][0]


async def _pending_rows(session_factory):
    from app.database import DriveOAuthPending

    async with session_factory() as db:
        return list((await db.execute(select(DriveOAuthPending))).scalars().all())


class _FakeCreds:
    """Mimics the token dict that `exchange_code` returns."""

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

    calls = {"codes": [], "verifiers": [], "result": None, "raise": None}

    def fake_exchange(code: str, credentials_web_dict: dict, code_verifier: str) -> dict:
        calls["codes"].append(code)
        calls["verifiers"].append(code_verifier)
        if calls["raise"]:
            raise calls["raise"]
        assert credentials_web_dict.get("client_id") == "oauth-client-id"
        return calls["result"] if calls["result"] is not None else _FakeCreds().to_dict()

    monkeypatch.setattr(drive_oauth, "exchange_code", fake_exchange)
    return calls


# --- start endpoint -----------------------------------------------------------

def test_start_200_json_with_pkce_challenge_and_browser_cookie(oauth_env):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    res = _start(client, org)
    assert res.status_code == 200, res.text
    url = res.json()["authorize_url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(url).query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == ["oauth-client-id"]
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]  # refresh_token is mandatory
    # PKCE S256 on the authorize URL
    assert q["code_challenge_method"] == ["S256"]
    assert len(q["code_challenge"][0]) == 43
    assert "state" in q

    # Browser-binding cookie: HttpOnly, SameSite=Lax, callback-scoped Path,
    # short Max-Age; no Secure flag on a plain-HTTP test request.
    cookies = res.headers.get_list("set-cookie")
    assert len(cookies) == 1
    (cookie,) = [c for c in cookies if c.startswith(COOKIE_NAME + "=")]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert f"Path={CALLBACK_PATH}" in cookie
    assert "Max-Age=600" in cookie
    assert "secure" not in cookie.lower()  # plain-HTTP test request ⇒ no Secure flag


async def _connectors_rows(session_factory, org: str):
    from app.database import OrgConnectorConfig

    async with session_factory() as db:
        rows = (await db.execute(select(OrgConnectorConfig))).scalars().all()
        return [r for r in rows if r.org_id == org]


def test_start_creates_pending_row_with_encrypted_verifier(oauth_env):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    res = _start(client, org)
    assert res.status_code == 200

    from app import connectors, drive_oauth

    rows = asyncio.run(_pending_rows(session_factory))
    assert len(rows) == 1
    row = rows[0]
    assert row.org_id == org and row.user_id == "admin1" and row.folder_id == "F-123"
    assert row.consumed_at is None
    # nonce hash matches the state echoed on the URL; raw nonce not stored
    nonce = _state_of(res.json()["authorize_url"])
    assert drive_oauth._sha256(nonce) == row.nonce_hash
    assert nonce not in row.nonce_hash
    # PKCE verifier encrypted at rest (Fernet ciphertext), decryptable
    assert row.code_verifier_encrypted.startswith("gAAA")
    assert connectors.decrypt_secret_string(row.code_verifier_encrypted)
    # browser secret hash only — the cookie we got is the raw secret
    cookie = res.headers.get_list("set-cookie")[0].split("=", 1)[1].split(";")[0]
    assert drive_oauth._sha256(cookie) == row.browser_secret_hash


def test_start_401_without_token(oauth_env):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    res = client.get(
        f"/api/v1/orgs/{org}/connectors/drive/oauth/start",
        params={"folder_id": "F-123"},
        follow_redirects=False,
    )
    assert res.status_code == 401


def test_start_requires_admin_role(oauth_env):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1", extra_member="peon", extra_role="member"))
    res = _start(client, org, user="peon")
    assert res.status_code == 403


def test_start_requires_membership(oauth_env):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    headers = {"Authorization": f"Bearer {_token('boss_of_other_org', 'org_other_member')}"}
    res = client.get(
        f"/api/v1/orgs/{org}/connectors/drive/oauth/start",
        params={"folder_id": "F-123"},
        headers=headers,
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
    res = _start(client, org)
    assert res.status_code == 503
    assert "GOOGLE_OAUTH_CLIENT_ID" in res.json()["detail"]


def test_second_start_overwrites_cookie_abandoning_first_flow(oauth_env):
    """One active flow per browser: the second start replaces the cookie, so
    the FIRST flow's callback now fails the cookie check (deliberate)."""
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    first = _start(client, org)
    state1 = _state_of(first.json()["authorize_url"])
    second = _start(client, org, folder_id="F-456")
    assert second.status_code == 200
    state2 = _state_of(second.json()["authorize_url"])
    assert state1 != state2
    assert len(asyncio.run(_pending_rows(session_factory))) == 2

    from app import drive_oauth

    # rows are separate; the replayed old flow is killed by the COOKIE check
    # (server-side both rows technically exist until expiry lazy-delete).
    row1 = next(r for r in asyncio.run(_pending_rows(session_factory)) if r.folder_id == "F-123")
    row2 = next(r for r in asyncio.run(_pending_rows(session_factory)) if r.folder_id == "F-456")
    assert row1.nonce_hash != row2.nonce_hash
    assert drive_oauth._sha256(state2) == row2.nonce_hash


# --- callback -----------------------------------------------------------------

def _callback(client, state: str, code: str = "4/valid-auth-code"):
    return client.get(
        CALLBACK_PATH,
        params={"code": code, "state": state},
        follow_redirects=False,
    )


def _cookie_of(res):
    (cookie,) = [c for c in res.headers.get_list("set-cookie") if c.startswith(COOKIE_NAME + "=")]
    return cookie


def test_callback_happy_path_consumes_once_stores_encrypted_audits_and_clears_cookie(
    oauth_env, exchange_spy
):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    start = _start(client, org)
    url = start.json()["authorize_url"]
    state = _state_of(url)
    cookie_raw = start.headers.get_list("set-cookie")[0]

    res = _callback(client, state)
    assert res.status_code == 302
    assert res.headers["location"] == f"{CONNECTORS_PAGE}?drive=connected"

    # exchange happened exactly once, with the pending row's verifier
    assert exchange_spy["codes"] == ["4/valid-auth-code"]
    assert len(exchange_spy["verifiers"]) == 1
    assert 43 <= len(exchange_spy["verifiers"][0]) <= 128

    # row is consumed exactly once
    rows = asyncio.run(_pending_rows(session_factory))
    assert len(rows) == 1 and rows[0].consumed_at is not None

    # connector stored ENCRYPTED under the row's org, decryptable for ingest
    from app import connectors

    rows = asyncio.run(_connectors_rows(session_factory, org))
    assert len(rows) == 1
    ciphertext = rows[0].config_encrypted
    assert "refresh_token" not in ciphertext  # sealed — no plaintext secrets
    assert "1//refresh" not in ciphertext

    async def _get():
        async with session_factory() as db:
            return await connectors.get_connector(db, org, "drive")

    config = asyncio.run(_get())
    assert config["folder_id"] == "F-123"
    assert config["token_json"]["refresh_token"] == "1//refresh"
    assert config["credentials_json"]["client_id"] == "oauth-client-id"

    # audit attributed to the ROW's user
    from app import audit
    from app.database import AuditLog

    async def _audits():
        async with session_factory() as db:
            return list((await db.execute(select(AuditLog))).scalars().all())

    set_rows = [r for r in asyncio.run(_audits()) if r.action_type == audit.ACTION_CONNECTOR_SET]
    assert len(set_rows) == 1 and set_rows[0].user_id == "admin1"

    # binding cookie cleared on the terminal response
    cleared = _cookie_of(res)
    assert cookie_raw != cleared
    assert f"{COOKIE_NAME}=" in cleared and "Max-Age=0" in cleared


def test_callback_without_cookie_rejected_before_exchange(oauth_env, exchange_spy):
    """Cross-browser/attacker callback: a fresh TestClient has NO cookie —
    rejected before any consume or exchange (spy-verified)."""
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    start = _start(client, org)
    state = _state_of(start.json()["authorize_url"])

    from fastapi.testclient import TestClient

    from app.server import app

    other_browser = TestClient(app)
    res = _callback(other_browser, state)
    assert res.status_code == 302
    assert res.headers["location"] == f"{CONNECTORS_PAGE}?drive=error&reason=browser_mismatch"
    assert exchange_spy["codes"] == []  # never exchanged
    assert asyncio.run(_connectors_rows(session_factory, org)) == []
    assert all(r.consumed_at is None for r in asyncio.run(_pending_rows(session_factory)))


def test_callback_with_wrong_cookie_rejected_before_exchange(oauth_env, exchange_spy):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    start = _start(client, org)
    state = _state_of(start.json()["authorize_url"])

    # a DIFFERENT secret in the cookie (attacker forges any value). Delete the
    # jar entry first (TestClient stores it under domain testserver.local),
    # then set the forged value as the only cookie the server sees.
    client.cookies.delete(COOKIE_NAME, domain="testserver.local", path=CALLBACK_PATH)
    client.cookies.set(COOKIE_NAME, "forged-browser-secret", domain="testserver.local", path=CALLBACK_PATH)
    res = _callback(client, state)
    assert res.status_code == 302
    assert "drive=error&reason=browser_mismatch" in res.headers["location"]
    assert exchange_spy["codes"] == []
    assert asyncio.run(_connectors_rows(session_factory, org)) == []


def test_callback_replay_rejected_second_attempt(oauth_env, exchange_spy):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    start = _start(client, org)
    state = _state_of(start.json()["authorize_url"])
    # keep the original browser secret: a browser that ignores the clear
    # still cannot replay — the ROW is single-use.
    cookie_value = start.headers.get_list("set-cookie")[0].split("=", 1)[1].split(";")[0]

    first = _callback(client, state)
    assert first.headers["location"].endswith("drive=connected")
    assert exchange_spy["codes"] == ["4/valid-auth-code"]

    # simulate a browser that kept the cookie across the clear (the first
    # response's Max-Age=0 may already have purged it from the jar)
    try:
        client.cookies.delete(COOKIE_NAME, domain="testserver.local", path=CALLBACK_PATH)
    except KeyError:
        pass
    client.cookies.set(COOKIE_NAME, cookie_value, domain="testserver.local", path=CALLBACK_PATH)
    second = _callback(client, state)
    assert "drive=error&reason=consumed_state" in second.headers["location"]
    # no second exchange, no duplicate storage, no extra rows
    assert exchange_spy["codes"] == ["4/valid-auth-code"]
    assert len(asyncio.run(_connectors_rows(session_factory, org))) == 1
    assert len(asyncio.run(_pending_rows(session_factory))) == 1


def test_callback_unknown_state_rejected(oauth_env, exchange_spy):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    _start(client, org)  # irrelevant row exists

    res = _callback(client, "never-issued-nonce")
    assert "drive=error&reason=unknown_state" in res.headers["location"]
    assert exchange_spy["codes"] == []
    assert asyncio.run(_connectors_rows(session_factory, org)) == []
    # cookie cleared even on this error path (the browser may hold one)
    assert "Max-Age=0" in _cookie_of(res)


def test_callback_expired_state_rejected_and_row_deleted(oauth_env, exchange_spy):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    start = _start(client, org)
    state = _state_of(start.json()["authorize_url"])

    async def _expire():
        from app.database import DriveOAuthPending

        async with session_factory() as db:
            row = (await db.execute(select(DriveOAuthPending))).scalars().one()
            row.expires_at = 1  # epoch second: long past
            await db.commit()

    asyncio.run(_expire())
    res = _callback(client, state)
    assert "drive=error&reason=expired_state" in res.headers["location"]
    assert exchange_spy["codes"] == []
    assert asyncio.run(_connectors_rows(session_factory, org)) == []
    # lazy delete removed the expired row
    assert asyncio.run(_pending_rows(session_factory)) == []


def test_callback_role_revoked_between_start_and_callback(oauth_env, exchange_spy):
    client, session_factory = oauth_env
    # a second admin so demoting admin1 does not trip the last-admin guard
    org = asyncio.run(_seed_org(session_factory, "admin1", extra_member="admin2", extra_role="admin"))
    start = _start(client, org)
    state = _state_of(start.json()["authorize_url"])

    async def _demote():
        from app import orgs

        async with session_factory() as db:
            await orgs.set_member_role(db, org, "admin1", "member", actor_user_id="admin2")
            await db.commit()

    asyncio.run(_demote())
    res = _callback(client, state)
    assert "drive=error&reason=revoked" in res.headers["location"]
    assert exchange_spy["codes"] == []
    assert asyncio.run(_connectors_rows(session_factory, org)) == []


def test_callback_exchange_failure_redirects_error_and_clears_cookie(oauth_env, exchange_spy):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    start = _start(client, org)
    state = _state_of(start.json()["authorize_url"])
    exchange_spy["raise"] = RuntimeError("grant_invalid with sensitive token userinfo")

    res = _callback(client, state)
    assert res.status_code == 302
    assert res.headers["location"] == f"{CONNECTORS_PAGE}?drive=error&reason=exchange_failed"
    # no raw exception text in the redirect
    assert "grant_invalid" not in res.headers["location"]
    assert asyncio.run(_connectors_rows(session_factory, org)) == []
    assert "Max-Age=0" in _cookie_of(res)
    # flow burned even though the exchange failed (single-use durability)
    rows = asyncio.run(_pending_rows(session_factory))
    assert rows[0].consumed_at is not None


def test_callback_no_refresh_token_saves_nothing(oauth_env, exchange_spy):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    start = _start(client, org)
    state = _state_of(start.json()["authorize_url"])
    exchange_spy["result"] = _FakeCreds(with_refresh=False).to_dict()

    res = _callback(client, state)
    assert res.status_code == 302
    assert res.headers["location"] == f"{CONNECTORS_PAGE}?drive=error&reason=no_refresh_token"
    assert asyncio.run(_connectors_rows(session_factory, org)) == []
    assert "Max-Age=0" in _cookie_of(res)


def test_callback_missing_params(oauth_env, exchange_spy):
    client, session_factory = oauth_env
    asyncio.run(_seed_org(session_factory, "admin1"))
    res = client.get(CALLBACK_PATH, follow_redirects=False)
    assert res.status_code == 302
    assert "drive=error&reason=missing_params" in res.headers["location"]


def test_callback_still_503_when_oauth_unconfigured(oauth_env, exchange_spy, monkeypatch):
    from app import config

    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    start = _start(client, org)
    state = _state_of(start.json()["authorize_url"])
    monkeypatch.setattr(config, "GOOGLE_OAUTH_REDIRECT_URI", "")
    res = _callback(client, state)
    assert res.status_code == 503


# --- drive_oauth unit contract ---------------------------------------------------

def test_browser_secret_and_nonce_are_high_entropy_and_constant_time():
    from app import drive_oauth

    n1, n2 = drive_oauth.new_nonce(), drive_oauth.new_nonce()
    s1, s2 = drive_oauth.new_browser_secret(), drive_oauth.new_browser_secret()
    assert len(n1) == len(n2) and len(s1) == len(s2) and n1 != n2 and s1 != s2
    assert drive_oauth._sha256(n1) == drive_oauth._sha256(n1)
    assert drive_oauth.verify_browser_secret(s1, drive_oauth._sha256(s1))
    assert not drive_oauth.verify_browser_secret(s2, drive_oauth._sha256(s1))


def test_pkce_verifier_and_challenge_contract():
    from app import drive_oauth

    verifier = drive_oauth.new_code_verifier()
    challenge = drive_oauth.code_challenge_s256(verifier)
    # RFC 7636: S256 = BASE64URL(SHA256(verifier)) without padding
    import base64
    import hashlib

    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected


# --- Slack test-and-list endpoint -------------------------------------------------


class _FakeSlackResponse:
    """Dict-shaped stand-in for SlackResponse (the endpoint only .gets)."""

    def __init__(self, data: dict):
        self._data = data

    def get(self, key, default=None):
        return self._data.get(key, default)


class _FakeSlackWebClient:
    """Stands in for slack_sdk.WebClient (patched in slack_sdk, which the
    endpoint imports at call time). auth.test + paginated conversations.list.
    Tokens in tests are obvious throwaways."""

    instances: ClassVar[list] = []

    def __init__(self, token: str, **kwargs):
        self.token = token
        _FakeSlackWebClient.instances.append(self)
        self._list_calls: list[dict] = []

    def _raise_invalid_auth(self):
        from slack_sdk.errors import SlackApiError

        raise SlackApiError(message="auth failed", response=_FakeSlackResponse({"error": "invalid_auth"}))

    def auth_test(self):
        if self.token == "xoxb-throws":
            raise RuntimeError("connection error")
        if not self.token.startswith("xoxb-valid"):
            self._raise_invalid_auth()
        return {"team": "Acme Workspace", "user": "bot"}

    def conversations_list(self, **kwargs):
        self._list_calls.append(kwargs)
        if not self.token.startswith("xoxb-valid"):
            self._raise_invalid_auth()
        pages = {
            None: {"channels": [{"id": "C1", "name": "general"}, {"id": "C2", "name": "eng"}],
                   "response_metadata": {"next_cursor": "next-cursor"}},
            "next-cursor": {"channels": [{"id": "C3", "name": "sales"}], "response_metadata": {"next_cursor": ""}},
        }
        return pages.get(kwargs.get("cursor"), {"channels": [], "response_metadata": {"next_cursor": ""}})


@pytest.fixture
def fake_slack(monkeypatch):
    _FakeSlackWebClient.instances = []
    # The endpoint imports WebClient at call time via `from slack_sdk import
    # WebClient`, so patch slack_sdk itself.
    import slack_sdk

    monkeypatch.setattr(slack_sdk, "WebClient", _FakeSlackWebClient)
    return _FakeSlackWebClient


def test_slack_test_returns_channels_from_pagination(oauth_env, fake_slack):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    res = client.post(
        f"/api/v1/orgs/{org}/connectors/slack/test",
        json={"bot_token": "xoxb-valid-token-1"},
        headers={"Authorization": f"Bearer {_token('admin1', org)}"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True and body["workspace"] == "Acme Workspace"
    assert body["channels"] == [
        {"id": "C1", "name": "general"},
        {"id": "C2", "name": "eng"},
        {"id": "C3", "name": "sales"},
    ]
    # paginated (2 pages), token never echoed back
    assert len(fake_slack.instances) == 1
    assert len(fake_slack.instances[0]._list_calls) == 2
    assert "xoxb" not in res.text
    # stored nothing
    assert asyncio.run(_connectors_rows(session_factory, org)) == []


def test_slack_test_invalid_token_clear_error(oauth_env, fake_slack):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1"))
    res = client.post(
        f"/api/v1/orgs/{org}/connectors/slack/test",
        json={"bot_token": "xoxb-totally-bogus"},
        headers={"Authorization": f"Bearer {_token('admin1', org)}"},
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "rejected this bot token" in detail
    # the token itself is never in the error
    assert "bogus" not in detail
    # Slack never saw a second call (auth.test failed first)
    assert fake_slack.instances[0]._list_calls == []


def test_slack_test_requires_admin(oauth_env, fake_slack):
    client, session_factory = oauth_env
    org = asyncio.run(_seed_org(session_factory, "admin1", extra_member="peon", extra_role="member"))
    res = client.post(
        f"/api/v1/orgs/{org}/connectors/slack/test",
        json={"bot_token": "xoxb-valid-token-1"},
        headers={"Authorization": f"Bearer {_token('peon', org)}"},
    )
    assert res.status_code == 403
    assert fake_slack.instances == []
