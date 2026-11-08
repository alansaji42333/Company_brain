"""Per-org connector credential store with encrypted-at-rest secrets.

Each org may store its own Slack and/or Google Drive connector config. The
full config JSON is encrypted with Fernet into a single Text column —
plaintext secrets (bot tokens, OAuth client secrets, OAuth token JSON) exist
in memory only while an ingest is running. They are never returned by
`get_connector_public` / `list_connectors_public`, never logged, and never
included in audit payloads.

Resolution precedence for ingest:
  org row → process env fallback → ValueError (nothing configured).

An org with no stored row therefore keeps the historical env/file behavior
of a single-tenant deployment, byte-for-byte.
"""
import base64
import hashlib
import json
import logging
import os

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import GOOGLE_DRIVE_FOLDER_ID, SLACK_BOT_TOKEN, SLACK_CHANNEL_IDS
from app.database import OrgConnectorConfig

logger = logging.getLogger(__name__)

SOURCES = ("slack", "drive")

_fernet: Fernet | None = None
_dev_fallback_warned = False

REQUIRED_FIELDS = {
    "slack": {
        "bot_token": str,       # secret
        "channel_ids": list,    # not secret, but part of the config
    },
    "drive": {
        "folder_id": str,       # not secret but org-specific
        "credentials_json": dict,  # secret (contains client_secret)
        "token_json": dict,        # secret (refresh_token)
    },
}


def _fernet_key() -> bytes:
    """Resolve the Fernet key: CONNECTOR_SECRETS_KEY, else a dev fallback
    derived from JWT_SECRET. Raises a clear error on first use when neither
    is configured (never at import time, so mere imports of this module stay
    safe in tests and tooling).
    """
    global _fernet, _dev_fallback_warned
    if _fernet is not None:
        return _fernet

    key_b64 = os.getenv("CONNECTOR_SECRETS_KEY", "")
    if key_b64:
        _fernet = Fernet(key_b64.encode())
        return _fernet

    # Dev fallback: derive a stable key from JWT_SECRET (sha256 → urlsafe).
    from app.config import JWT_SECRET
    if JWT_SECRET:
        digest = hashlib.sha256(("connector-secrets:" + JWT_SECRET).encode()).digest()
        if not _dev_fallback_warned:
            logger.warning(
                "CONNECTOR_SECRETS_KEY not set: connector secrets are encrypted "
                "with a key derived from JWT_SECRET (dev-grade). Set "
                "CONNECTOR_SECRETS_KEY for production."
            )
            _dev_fallback_warned = True
        _fernet = Fernet(base64.urlsafe_b64encode(digest))
        return _fernet

    raise RuntimeError(
        "Cannot encrypt or decrypt connector credentials: set CONNECTOR_SECRETS_KEY "
        "(a Fernet key from `cryptography.fernet.Fernet.generate_key()`), or set "
        "JWT_SECRET from which a dev-grade key is derived."
    )


def _encrypt(config: dict) -> str:
    plaintext = json.dumps(config, separators=(",", ":")).encode()
    return _fernet_key().encrypt(plaintext).decode()


def _decrypt(ciphertext: str) -> dict:
    try:
        plaintext = _fernet_key().decrypt(ciphertext.encode())
    except InvalidToken as e:
        raise ValueError(
            "Stored connector config cannot be decrypted with the current key "
            "(was CONNECTOR_SECRETS_KEY rotated or the row written by another "
            "deployment?). Re-save the connector."
        ) from e
    return json.loads(plaintext)


def _validate(source: str, config: dict) -> None:
    if source not in SOURCES:
        raise ValueError(f"Unknown connector source: {source!r} (expected one of {SOURCES})")
    if not isinstance(config, dict):
        # ValueError (not TypeError) everywhere here: invalid connector input
        # is a user-facing 400, matching orgs.py's ValueError-on-invalid-role.
        raise ValueError("Connector config must be an object/dict")  # noqa: TRY004 — see above

    required = REQUIRED_FIELDS[source]
    for field, expected in required.items():
        value = config.get(field)
        missing = value is None or value == "" or value == []
        if missing or not isinstance(value, expected):
            raise ValueError(
                f"Connector config for {source} requires {field!r} "
                f"of type {expected.__name__}"
            )
    if source == "slack" and any(not isinstance(c, str) or not c.strip() for c in config["channel_ids"]):
        raise ValueError("channel_ids must be a list of non-empty strings")


# --- persistence -----------------------------------------------------------

async def _row(db: AsyncSession, org_id: str, source: str) -> OrgConnectorConfig | None:
    result = await db.execute(
        select(OrgConnectorConfig).where(
            OrgConnectorConfig.org_id == org_id,
            OrgConnectorConfig.source == source,
        )
    )
    return result.scalar_one_or_none()


async def set_connector(db: AsyncSession, org_id: str, source: str, config: dict, user_id: str) -> None:
    """Validate, encrypt and upsert the org's connector config for `source`.

    The caller owns the transaction (commit); this function flushes so
    uniqueness violations surface before the commit.
    """
    _validate(source, config)
    row = await _row(db, org_id, source)
    if row is None:
        row = OrgConnectorConfig(org_id=org_id, source=source)
        db.add(row)
    row.config_encrypted = _encrypt(config)
    row.updated_by = user_id
    await db.flush()
    logger.info("Connector %s set for org %s (updated_by=%s)", source, org_id, user_id)


async def get_connector(db: AsyncSession, org_id: str, source: str) -> dict | None:
    """The org's DECRYPTED config for `source`, for ingest use only.

    Never expose the return value through any API response.
    """
    if source not in SOURCES:
        raise ValueError(f"Unknown connector source: {source!r} (expected one of {SOURCES})")
    row = await _row(db, org_id, source)
    if row is None:
        return None
    return _decrypt(row.config_encrypted)


async def get_connector_public(db: AsyncSession, org_id: str, source: str) -> dict | None:
    """Non-secret view of a stored connector, safe for API responses.

    Contains: source, configured=True, folder_id / channel_ids (NOT the bot
    token, NOT credentials_json, NOT token_json), updated_at, updated_by.
    """
    if source not in SOURCES:
        raise ValueError(f"Unknown connector source: {source!r} (expected one of {SOURCES})")
    config = await get_connector(db, org_id, source)
    if config is None:
        return None

    public: dict = {"source": source, "configured": True}
    if source == "slack":
        public["channel_ids"] = list(config.get("channel_ids") or [])
    else:
        public["folder_id"] = config.get("folder_id", "")
    row = await _row(db, org_id, source)
    public["updated_at"] = row.updated_at
    public["updated_by"] = row.updated_by
    return public


async def list_connectors_public(db: AsyncSession, org_id: str) -> list[dict]:
    """Public views of every connector configured for the org."""
    views = []
    for source in SOURCES:
        public = await get_connector_public(db, org_id, source)
        if public:
            views.append(public)
    return views


async def delete_connector(db: AsyncSession, org_id: str, source: str) -> bool:
    """Remove the org's connector config for `source`. True if a row existed."""
    if source not in SOURCES:
        raise ValueError(f"Unknown connector source: {source!r} (expected one of {SOURCES})")
    row = await _row(db, org_id, source)
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    logger.info("Connector %s deleted for org %s", source, org_id)
    return True


# --- resolution (ingest path) ----------------------------------------------

def resolve_slack_config(org_cfg: dict | None) -> tuple[str, list[str]]:
    """Effective Slack config for an ingest: (bot_token, channel_ids).

    Precedence: org connector config → env (SLACK_BOT_TOKEN / SLACK_CHANNEL_IDS)
    → ValueError. The org config's channel ids are the ONLY channels ingested.
    """
    if org_cfg:
        token = org_cfg.get("bot_token") or ""
        ids = org_cfg.get("channel_ids") or []
        if token:
            return token, [str(c) for c in ids if str(c).strip()]
        raise ValueError("Org slack connector config has an empty bot_token")

    token = SLACK_BOT_TOKEN
    ids = [c.strip() for c in SLACK_CHANNEL_IDS.split(",") if c.strip()] if SLACK_CHANNEL_IDS else []
    if not token:
        raise ValueError("no slack connector configured")
    if not ids:
        raise ValueError("no slack connector configured (no channels)")
    return token, ids


def resolve_drive_config(
    org_cfg: dict | None, folder_id_override: str | None,
) -> tuple[str, dict | None, dict | None]:
    """Effective Drive config for an ingest: (folder_id, credentials_json, token_json).

    Precedence for folder_id: folder_id_override → org config → env
    (GOOGLE_DRIVE_FOLDER_ID). credentials_json/token_json come from the org
    config or are None, meaning: use the existing credentials.json/token.json
    file / GOOGLE_* env behavior (single-tenant fallback).

    Raises ValueError when nothing resolves (no org config, no override, no env).
    """
    if org_cfg:
        folder = folder_id_override or org_cfg.get("folder_id") or ""
        if folder:
            return folder, org_cfg.get("credentials_json"), org_cfg.get("token_json")
        raise ValueError("Org drive connector config has an empty folder_id")

    folder = folder_id_override or GOOGLE_DRIVE_FOLDER_ID
    if not folder:
        raise ValueError("no drive connector configured")
    return folder, None, None
