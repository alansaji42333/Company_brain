"""Hermetic per-org connector tests (tenant credential onboarding).

No network, no real Chroma/Redis/Postgres/Slack/Google: the Chroma collection
surface and embed are faked (same seams as tests/test_source_freshness.py,
copied not imported), the DB is a tmp sqlite engine patched into
app.database, the Slack/Google boundaries are monkeypatched at
slack_ingest.WebClient / google_auth.get_credentials / drive_ingest seams.

Focus:
  1. crypto round-trip + tamper detection            5. drive boundary + refresh persist
  2. masked public views                              6. nothing-configured → error result
  3. slack worker uses the ORG token                  7. per-org isolation
  4. env fallback back-compat
  8. admin/masked API behavior
"""
import asyncio
import json
from typing import ClassVar

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# ---------------------------------------------------------------------
# fake vector store (copied from tests/test_source_freshness.py)
# ---------------------------------------------------------------------

class FakeCollection:
    """In-memory Chroma surface: get(ids=, where=), delete(ids), upsert(...), count()."""

    def __init__(self):
        self.docs: dict[str, dict] = {}  # id -> {"document": str, "metadata": dict}

    def get(self, ids=None, where=None, include=None):
        selected = list(self.docs.items())
        if ids is not None:
            wanted = set(ids)
            selected = [(i, d) for i, d in selected if i in wanted]
        if where:
            def matches(metadata: dict) -> bool:
                return all(metadata.get(k) == v for k, v in where.items())

            selected = [(i, d) for i, d in selected if matches(d["metadata"])]
        return {
            "ids": [i for i, _ in selected],
            "metadatas": [d["metadata"] for _, d in selected],
            "documents": [d["document"] for _, d in selected],
        }

    def delete(self, ids):
        for i in ids:
            self.docs.pop(i, None)

    def upsert(self, ids, embeddings, documents, metadatas):
        for i, doc, meta in zip(ids, documents, metadatas):
            self.docs[i] = {"document": doc, "metadata": meta}

    def count(self) -> int:
        return len(self.docs)


@pytest.fixture
def fake_store(monkeypatch):
    """Fake Chroma collections + embed recorder, keyed by collection name."""
    from app import vectorstore

    collections: dict[str, FakeCollection] = {}

    def fake_get_collection(name=vectorstore.COLLECTION_NAME):
        return collections.setdefault(name, FakeCollection())

    embed_calls: list[list[str]] = []

    def fake_embed(texts):
        embed_calls.append(list(texts))
        return [[0.0] * 4 for _ in texts]

    monkeypatch.setattr(vectorstore, "_get_collection", fake_get_collection)
    monkeypatch.setattr(vectorstore, "embed", fake_embed)
    collections.setdefault(vectorstore.COLLECTION_NAME, FakeCollection())
    return collections, embed_calls


def make_drive_doc(doc_id: str, text: str, org_id: str) -> dict:
    return {
        "id": doc_id, "name": f"{doc_id}.txt", "mime_type": "text/plain", "text": text,
        "source_url": "", "source_type": "drive",
    }


def make_slack_chunks_for(channel_id: str, texts: list[str], org_id: str) -> list[dict]:
    return [
        {
            "doc_id": f"slack_{channel_id}", "doc_name": f"#{channel_id}", "source_url": "",
            "chunk_index": i, "text": t, "source_type": "slack", "user_id": "", "org_id": org_id,
        }
        for i, t in enumerate(texts)
    ]


# ---------------------------------------------------------------------
# tmp sqlite DB patched into app.database (worker + store tests)
# ---------------------------------------------------------------------

@pytest.fixture
def tenant_db(monkeypatch, tmp_path):
    from app import database

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tenant.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "async_session", session_factory)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)

    asyncio.run(_create())
    yield database, session_factory
    asyncio.run(engine.dispose())


def db_session(tenant_db):
    return tenant_db[1]()


async def seed_org_connector(db, org_id: str, source: str, config: dict, user_id: str = "admin1"):
    from app import connectors
    await connectors.set_connector(db, org_id, source, config, user_id)
    await db.commit()


# =====================================================================
# 1. crypto round-trip + tamper detection
# =====================================================================

def test_crypto_roundtrip_and_tamper_detection(tenant_db, monkeypatch):
    from cryptography.fernet import Fernet

    from app import connectors

    key = Fernet.generate_key()
    monkeypatch.setenv("CONNECTOR_SECRETS_KEY", key.decode())
    connectors._fernet = None  # force re-resolution with the test key

    plaintext = {"bot_token": "xoxb-super-secret-token", "channel_ids": ["C123"]}
    ciphertext = connectors._encrypt(plaintext)

    assert "xoxb-super-secret-token" not in ciphertext
    assert "channel_ids" not in ciphertext
    assert connectors._decrypt(ciphertext) == plaintext

    tampered = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
    with pytest.raises(ValueError):
        connectors._decrypt(tampered)

    connectors._fernet = None  # don't leak the test key into other tests


def test_no_key_configured_raises_runtimeerror_on_first_use(monkeypatch):
    from app import connectors

    monkeypatch.delenv("CONNECTOR_SECRETS_KEY", raising=False)
    monkeypatch.setattr("app.config.JWT_SECRET", "")
    monkeypatch.setattr(connectors, "_fernet", None)

    with pytest.raises(RuntimeError, match="CONNECTOR_SECRETS_KEY"):
        connectors._encrypt({"x": 1})

    connectors._fernet = None


# =====================================================================
# 2. set/get/delete + masked public views
# =====================================================================

async def _public_views_roundtrip(db, org_id):
    from app import connectors
    await connectors.set_connector(db, org_id, "slack", {
        "bot_token": "xoxb-live-secret", "channel_ids": ["C1"],
    }, "boss")
    await connectors.set_connector(db, org_id, "drive", {
        "folder_id": "folderABC", "credentials_json": {"client_secret": "TOPSECRET"},
        "token_json": {"refresh_token": "REFRESHTOKEN"},
    }, "boss")
    await db.commit()

    # Ingest-use retrieval decrypts fully.
    slack_cfg = await connectors.get_connector(db, org_id, "slack")
    assert slack_cfg == {"bot_token": "xoxb-live-secret", "channel_ids": ["C1"]}

    # Public view + list: no secrets anywhere in serialized form.
    public = await connectors.get_connector_public(db, org_id, "slack")
    drive_public = await connectors.get_connector_public(db, org_id, "drive")
    listed = await connectors.list_connectors_public(db, org_id)
    blob = json.dumps([public, drive_public, listed], default=str)
    for secret in ("xoxb-live-secret", "TOPSECRET", "REFRESHTOKEN", "client_secret", "refresh_token"):
        assert secret not in blob, f"secret {secret!r} leaked through a public view"

    assert public["configured"] is True and public["channel_ids"] == ["C1"]
    assert drive_public["configured"] is True and drive_public["folder_id"] == "folderABC"

    # Row inspection: the DB column holds ciphertext, not the plaintext token.
    from sqlalchemy import select

    from app.database import OrgConnectorConfig
    rows = (await db.execute(select(OrgConnectorConfig.__table__))).fetchall()
    stored = [r._mapping["config_encrypted"] for r in rows
              if r._mapping["org_id"] == org_id and r._mapping["source"] == "slack"]
    assert len(stored) == 1
    assert "xoxb-live-secret" not in stored[0]

    # Resolution precedence (stored config wins regardless of any binding).
    from app import connectors as c
    token, ids = c.resolve_slack_config(org_id, slack_cfg)
    assert (token, ids) == ("xoxb-live-secret", ["C1"])
    folder, creds, tok = c.resolve_drive_config(
        org_id, await c.get_connector(db, org_id, "drive"), None,
    )
    assert (folder, creds, tok) == ("folderABC", {"client_secret": "TOPSECRET"}, {"refresh_token": "REFRESHTOKEN"})

    # Delete removes the row; resolution then stops (or uses env only for an
    # explicitly bound legacy org) — see the dedicated legacy-binding tests.
    assert await connectors.delete_connector(db, org_id, "slack") is True
    assert await connectors.delete_connector(db, org_id, "slack") is False
    await db.commit()
    assert await connectors.get_connector(db, org_id, "slack") is None


def test_set_get_delete_and_masked_views(tenant_db):
    db = db_session(tenant_db)
    asyncio.run(_public_views_roundtrip(db, "org_store1"))


# =====================================================================
# Slack/Google boundary fakes for worker tests
# =====================================================================

class FakeWebClient:
    """Records constructed tokens; serves canned per-channel history."""

    constructed: ClassVar[list] = []
    history: ClassVar[dict] = {}
    names: ClassVar[dict] = {}

    def __init__(self, token=""):
        FakeWebClient.constructed.append(token)
        self.token = token

    def conversations_join(self, channel):
        return {"ok": True}

    def conversations_info(self, channel):
        return {"channel": {"name": FakeWebClient.names.get(channel, channel)}}

    def conversations_history(self, channel, **kw):
        return {"messages": FakeWebClient.history.get(channel, [])}

    def conversations_replies(self, channel, ts, **kw):
        return {"messages": []}

    def chat_getPermalink(self, channel, message_ts):
        return {"permalink": ""}

    def users_info(self, user):
        return {"user": {"profile": {"display_name": "Alice"}}}


@pytest.fixture
def slack_env(monkeypatch):
    """Patch the slack WebClient seam and reset module caches per test."""
    from app import slack_ingest

    FakeWebClient.constructed = []
    FakeWebClient.history = {}
    FakeWebClient.names = {}
    monkeypatch.setattr(slack_ingest, "WebClient", FakeWebClient)
    monkeypatch.setattr(slack_ingest, "_client", None)
    monkeypatch.setattr(slack_ingest, "_client_cache", {})
    monkeypatch.setattr(slack_ingest, "_username_cache", {})
    slack_ingest.global_state_reset()
    return slack_ingest, FakeWebClient


@pytest.fixture
def drive_env(monkeypatch):
    """Patch the google_auth + drive_ingest seams, recording what arrived."""
    from app import connectors, drive_ingest, google_auth

    records = {
        "credentials_calls": [],   # (credentials_json, token_dict)
        "services": [],            # credentials passed to get_drive_service
        "folders": [],             # folder ids that reached the ingest boundary
        "refreshed": [],           # token dicts handed to on_token_refresh
    }
    creds_sentinel = object()

    def fake_get_credentials(credentials_json=None, token_dict=None, on_token_refresh=None):
        records["credentials_calls"].append((credentials_json, token_dict))
        records["refreshed"].append(on_token_refresh)
        return creds_sentinel

    def fake_get_drive_service(credentials=None):
        records["services"].append(credentials)
        return object()

    def fake_list_files_in_folder(service, folder_id):
        records["folders"].append(folder_id)
        return [{"id": "file1", "name": "notes.txt", "mimeType": "text/plain", "webViewLink": ""}]

    def fake_extract_text(file_info, service):
        return make_drive_doc(file_info["id"], f"content of {file_info['id']}", org_id="")

    monkeypatch.setattr(google_auth, "get_credentials", fake_get_credentials)
    monkeypatch.setattr(drive_ingest, "get_drive_service", fake_get_drive_service)
    monkeypatch.setattr(drive_ingest, "list_files_in_folder", fake_list_files_in_folder)
    monkeypatch.setattr(drive_ingest, "extract_text", fake_extract_text)
    # resolve_drive_config reads this module attr for the env fallback — a
    # distinctive value proving/views it is ignored when an org config exists.
    # (drive_ingest has no such module attr; its local env read lives downstream.)
    monkeypatch.setattr(connectors, "GOOGLE_DRIVE_FOLDER_ID", "ENV-FOLDER-IGNORED")
    return records, creds_sentinel


def run_slack_job(org_id: str, user_id: str = "scheduler") -> dict:
    from app.worker import ingest_slack
    return asyncio.run(ingest_slack({}, user_id=user_id, org_id=org_id))


def run_drive_job(org_id: str, folder_id: str | None = None, user_id: str = "scheduler") -> dict:
    from app.worker import ingest_drive
    return asyncio.run(ingest_drive({}, folder_id=folder_id, user_id=user_id, org_id=org_id))


# =====================================================================
# 3. slack worker uses the ORG's token
# =====================================================================

def test_slack_worker_uses_org_token_not_env(tenant_db, fake_store, slack_env, monkeypatch):
    _, FakeWebClient = slack_env
    database, _ = tenant_db
    collections, _ = fake_store

    # A distinctive env token that must NOT be used (bound-bind-free env values).
    from app import connectors as c
    monkeypatch.setattr(c, "LEGACY_CONNECTOR_ORG_ID", "")  # env can never apply here anyway
    monkeypatch.setattr(c, "SLACK_BOT_TOKEN", "xoxb-ENV-SHOULD-NOT-BE-USED")
    monkeypatch.setattr(c, "SLACK_CHANNEL_IDS", "C-ENV-IGNORED")
    FakeWebClient.history["C123"] = [{"ts": "100.000", "user": "U1", "text": "hello org A"}]

    async def _seed():
        async with database.async_session() as db:
            await seed_org_connector(db, "org_slackA", "slack", {
                "bot_token": "xoxb-ORG-A-TOKEN",
                "channel_ids": ["C123"],
            })

    asyncio.run(_seed())

    result = run_slack_job("org_slackA")

    assert FakeWebClient.constructed == ["xoxb-ORG-A-TOKEN"], "the org token must be the only WebClient built"
    assert result["channels"] == 1
    assert result["chunks_stored"] == 1

    # Chunks live in the main collection, org-scoped, from the org channel.
    from app.vectorstore import COLLECTION_NAME
    fake = collections[COLLECTION_NAME]
    metas = fake.get(where={"org_id": "org_slackA"})["metadatas"]
    assert len(metas) == 1
    assert metas[0]["channel_name"] == "C123"
    assert "hello org A" in fake.get()["documents"][0]


# =====================================================================
# 4. explicitly bound legacy org → env fallback (back-compat)
# =====================================================================

def test_slack_worker_legacy_bound_org_uses_env_fallback(tenant_db, fake_store, slack_env, monkeypatch):
    """Back-compat: an org WITHOUT a stored connector may use the global env
    credentials ONLY when it is the explicit LEGACY_CONNECTOR_ORG_ID."""
    from app import connectors as c

    _, FakeWebClient = slack_env
    collections, _ = fake_store

    # Explicitly bind THIS org (restored by monkeypatch after the test).
    monkeypatch.setattr(c, "LEGACY_CONNECTOR_ORG_ID", "org_legacy")
    monkeypatch.setattr(c, "SLACK_BOT_TOKEN", "xoxb-ENV-TOKEN")
    monkeypatch.setattr(c, "SLACK_CHANNEL_IDS", "C-env")
    FakeWebClient.history["C-env"] = [{"ts": "200.000", "user": "U9", "text": "env path works"}]

    result = run_slack_job("org_legacy")  # no connector row — but explicitly bound

    assert FakeWebClient.constructed == ["xoxb-ENV-TOKEN"], "bound org must fall back to env"
    assert result["channels"] == 1
    from app.vectorstore import COLLECTION_NAME
    metas = collections[COLLECTION_NAME].get(where={"org_id": "org_legacy"})["metadatas"]
    assert len(metas) == 1
    assert metas[0]["doc_id"] == "slack_C-env"


def test_unbound_org_never_reaches_env_fallback_slack(tenant_db, fake_store, slack_env, monkeypatch):
    """Security contract: an unconfigured, NON-bound org must never reach the
    global credentials — even when the env has valid-looking values."""
    from app import connectors as c

    _, FakeWebClient = slack_env
    collections, _ = fake_store

    monkeypatch.setattr(c, "LEGACY_CONNECTOR_ORG_ID", "")  # unset binding
    monkeypatch.setattr(c, "SLACK_BOT_TOKEN", "xoxb-GLOBAL-NOT-FOR-THIS-ORG")
    monkeypatch.setattr(c, "SLACK_CHANNEL_IDS", "C-global")
    FakeWebClient.history["C-global"] = [{"ts": "200.000", "user": "U9", "text": "must not appear"}]

    result = run_slack_job("org_unbound")

    assert result["error"] == "no slack connector configured for org org_unbound"
    assert result["channels"] == 0 and result["chunks_stored"] == 0
    assert FakeWebClient.constructed == [], "no WebClient may be built for an unbound org"
    from app.vectorstore import COLLECTION_NAME
    assert collections[COLLECTION_NAME].count() == 0


# =====================================================================
# 5. drive: org creds reach the google_auth boundary; refresh persists
# =====================================================================

def test_drive_worker_uses_org_config_and_persists_refresh(tenant_db, fake_store, drive_env, monkeypatch):
    database, _ = tenant_db
    records, _cred_sentinel = drive_env

    org_creds = {"client_secret": "ORG-CLIENT-SECRET"}
    org_token = {"refresh_token": "ORG-REFRESH", "token": "stale-access"}
    org_folder = "ORG-FOLDER-ID"

    async def _seed():
        async with database.async_session() as db:
            await seed_org_connector(db, "org_driveA", "drive", {
                "folder_id": org_folder,
                "credentials_json": org_creds,
                "token_json": org_token,
            })

    asyncio.run(_seed())

    NEW_TOKEN = {"refresh_token": "ORG-REFRESH", "token": "ACCESS-TOKEN-ROTATED"}

    # Kick off the job; the google_auth seam fires on_token_refresh AFTER the
    # credentials are resolved (simulating a successful refresh inside Google's
    # transport) — the worker must persist the new token dict.
    def refresh_then_return(credentials_json=None, token_dict=None, on_token_refresh=None):
        records["credentials_calls"].append((credentials_json, token_dict))
        if on_token_refresh is not None:
            on_token_refresh(NEW_TOKEN)
        return object()

    from app import google_auth
    monkeypatch.setattr(google_auth, "get_credentials", refresh_then_return)

    result = run_drive_job("org_driveA")

    assert result["ingested"] == 1
    # The org's dicts reached the google_auth boundary...
    assert (org_creds, org_token) in records["credentials_calls"]
    # ...and the ENV folder did NOT: the org folder_id drove the listing.
    assert records["folders"] == [org_folder]
    assert org_folder != "ENV-FOLDER-IGNORED"

    # The refreshed token was persisted (re-encrypted) into the org's row.
    async def _read_back():
        async with database.async_session() as db:
            from app import connectors
            config = await connectors.get_connector(db, "org_driveA", "drive")
            return config

    stored = asyncio.run(_read_back())
    assert stored["token_json"] == NEW_TOKEN
    assert stored["credentials_json"] == org_creds


# =====================================================================
# 6. nothing configured anywhere → clear error, nothing stored/deleted
# =====================================================================

def test_no_connector_anywhere_returns_clear_error(tenant_db, fake_store, slack_env, monkeypatch):
    from app import connectors as c

    _, _Fake = slack_env
    _collections, _ = fake_store  # nothing may be stored; store integrity via empty result

    monkeypatch.setattr(c, "LEGACY_CONNECTOR_ORG_ID", "")
    monkeypatch.setattr(c, "SLACK_BOT_TOKEN", "")
    monkeypatch.setattr(c, "SLACK_CHANNEL_IDS", "")

    result = run_slack_job("org_untouched_1")
    assert result["error"] == "no slack connector configured for org org_untouched_1"
    assert result["chunks_stored"] == 0 and result["chunks_deleted"] == 0


def test_no_drive_connector_anywhere_returns_clear_error(tenant_db, fake_store, drive_env, monkeypatch):
    from app import connectors as connectors_module

    # Nothing configured: no override, no org row, and no env folder.
    monkeypatch.setattr(connectors_module, "GOOGLE_DRIVE_FOLDER_ID", "")

    result = run_drive_job("org_untouched_2", folder_id=None)

    assert result["error"] == "no drive connector configured for org org_untouched_2"
    assert result["chunks_stored"] == 0 and result["files_processed"] == 0
    # The google_auth boundary was never reached (resolution failed first).
    assert drive_env[0]["credentials_calls"] == []


# =====================================================================
# 7. org isolation: A's connector is never used for B's job
# =====================================================================

def test_per_org_isolation_both_orgs_configured(tenant_db, fake_store, slack_env, drive_env):
    database, _ = tenant_db
    _, FakeWebClient = slack_env
    FakeWebClient.history["C-A"] = [{"ts": "300.000", "user": "U1", "text": "org A dialogue"}]
    FakeWebClient.history["C-B"] = [{"ts": "300.000", "user": "U2", "text": "org B dialogue"}]

    async def _seed():
        async with database.async_session() as db:
            await seed_org_connector(db, "org_iso_A", "slack", {
                "bot_token": "xoxb-TOKEN-A", "channel_ids": ["C-A"],
            })
            await seed_org_connector(db, "org_iso_B", "slack", {
                "bot_token": "xoxb-TOKEN-B", "channel_ids": ["C-B"],
            })

    asyncio.run(_seed())

    run_slack_job("org_iso_A")
    # Only A's token was constructed for A's job.
    assert FakeWebClient.constructed == ["xoxb-TOKEN-A"]
    run_slack_job("org_iso_B")
    assert FakeWebClient.constructed == ["xoxb-TOKEN-A", "xoxb-TOKEN-B"]  # B's token added, A's not reused as env

    # Drive: same story with folders via the drive seams.
    async def _seed_drive():
        async with database.async_session() as db:
            await seed_org_connector(db, "org_iso_A", "drive", {
                "folder_id": "FOLDER-A", "credentials_json": {"client_secret": "sA"}, "token_json": {"t": "tA"},
            })
            await seed_org_connector(db, "org_iso_B", "drive", {
                "folder_id": "FOLDER-B", "credentials_json": {"client_secret": "sB"}, "token_json": {"t": "tB"},
            })

    asyncio.run(_seed_drive())

    run_drive_job("org_iso_A")
    run_drive_job("org_iso_B")
    assert drive_env[0]["folders"] == ["FOLDER-A", "FOLDER-B"]
    creds_seen = [dict(cj or {}) for (cj, _tj) in drive_env[0]["credentials_calls"]]
    assert {"client_secret": "sA"} in creds_seen and {"client_secret": "sB"} in creds_seen


# =====================================================================
# 8. API: admin PUT, member 403, masked GET body, DELETE → env fallback
# =====================================================================

def _make_token(secret: str, user_id: str, org_id: str | None = None) -> str:
    from jose import jwt
    payload = {"sub": user_id}
    if org_id:
        payload["org_id"] = org_id
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture
def api_env(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app import auth, config, database

    monkeypatch.setattr(config, "JWT_SECRET", "test-secret")
    monkeypatch.setattr(auth, "JWT_SECRET", "test-secret")
    monkeypatch.setattr(config, "AUTH0_ENABLED", False)
    monkeypatch.setattr(auth, "AUTH0_ENABLED", False)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "async_session", session_factory)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)

    asyncio.run(_create())

    from app.server import app
    app.state.redis = None  # connector endpoints don't need the pool
    client = TestClient(app)
    try:
        yield client, session_factory
    finally:
        asyncio.run(engine.dispose())


def _token(secret: str, user_id: str, org_id: str) -> str:
    return _make_token(secret, user_id, org_id)


async def _seed_org(db, member_role: str = "member") -> str:
    """Create a tenant org: 'boss1' becomes admin, a second user `member_role`."""
    from app.orgs import ROLE_ADMIN, ROLE_MEMBER, add_member, create_org

    assert {ROLE_ADMIN, ROLE_MEMBER} == {"admin", "member"}
    org = await create_org(db, name="Tenant", slug=None, owner_user_id="boss1")
    await add_member(db, org["id"], "underling", member_role)
    await db.commit()
    return org["id"]


def test_api_roundtrip(api_env):
    client, session_factory = api_env

    from app import config

    async def _seed():
        async with session_factory() as db:
            return await _seed_org(db)

    org_id = asyncio.run(_seed())
    headers_boss = {"Authorization": f"Bearer {_token(config.JWT_SECRET, 'boss1', org_id)}"}
    headers_member = {"Authorization": f"Bearer {_token(config.JWT_SECRET, 'underling', org_id)}"}

    SECRET_TOKEN = "xoxb-API-SECRET-TOKEN"
    res = client.put(
        f"/api/v1/orgs/{org_id}/connectors/slack",
        json={"bot_token": SECRET_TOKEN, "channel_ids": ["C-api", "C-api-2"]},
        headers=headers_boss,
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"org_id": org_id, "source": "slack", "configured": True}

    # GET returns a masked view: no secret substring anywhere in the body.
    res = client.get(f"/api/v1/orgs/{org_id}/connectors", headers=headers_boss)
    assert res.status_code == 200
    body = res.text
    assert SECRET_TOKEN not in body
    assert "bot_token" not in body
    assert any(c["source"] == "slack" and c["channel_ids"] == ["C-api", "C-api-2"] for c in res.json()["connectors"])

    # Member role cannot write (admin-only) — 403, row unchanged.
    res = client.put(
        f"/api/v1/orgs/{org_id}/connectors/slack",
        json={"bot_token": "xoxb-sneaky", "channel_ids": ["C-x"]},
        headers=headers_member,
    )
    assert res.status_code == 403

    # Drive PUT + list shows both.
    res = client.put(
        f"/api/v1/orgs/{org_id}/connectors/drive",
        json={"folder_id": "F-API", "credentials_json": {"client_secret": "API-CS"},
              "token_json": {"refresh_token": "API-RT"}},
        headers=headers_boss,
    )
    assert res.status_code == 200, res.text
    res = client.get(f"/api/v1/orgs/{org_id}/connectors", headers=headers_boss)
    body = res.text
    assert "API-CS" not in body and "API-RT" not in body
    assert any(c["source"] == "drive" and c["folder_id"] == "F-API" for c in res.json()["connectors"])

    # DELETE removes the row; subsequent resolution falls back to env.
    res = client.delete(f"/api/v1/orgs/{org_id}/connectors/drive", headers=headers_boss)
    assert res.status_code == 200, res.text
    assert res.json()["removed"] is True

    from app import connectors

    async def _after_delete():
        async with session_factory() as db:
            stored = await connectors.get_connector(db, org_id, "drive")
            public = await connectors.list_connectors_public(db, org_id)
            return stored, [p["source"] for p in public]

    stored, sources = asyncio.run(_after_delete())
    assert stored is None
    assert sources == ["slack"]  # only the slack row remains

    # DELETE again → 404 (already gone)
    res = client.delete(f"/api/v1/orgs/{org_id}/connectors/drive", headers=headers_boss)
    assert res.status_code == 404

    # Audit rows: connector_set/connector_deleted, config never in payload.
    from sqlalchemy import select

    from app.database import AuditLog

    async def _audit_rows():
        async with session_factory() as db:
            rows = await db.execute(select(AuditLog))
            return [(r.action_type, r.payload) for r in rows.scalars()]

    audit_rows = asyncio.run(_audit_rows())
    assert ("connector_set", {"source": "slack"}) in audit_rows
    assert ("connector_set", {"source": "drive"}) in audit_rows
    assert ("connector_deleted", {"source": "drive"}) in audit_rows
    for _action, payload in audit_rows:
        assert SECRET_TOKEN not in json.dumps(payload or {})
