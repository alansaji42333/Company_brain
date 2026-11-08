import json
import logging
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from app.config import CREDENTIALS_FILE, SCOPES, TOKEN_FILE

logger = logging.getLogger(__name__)


def _get_credentials_json() -> dict | None:
    if os.path.exists(CREDENTIALS_FILE):
        with open(CREDENTIALS_FILE) as f:
            return json.load(f)
    env_val = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if env_val:
        return json.loads(env_val)
    return None


def _get_token_dict() -> dict | None:
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return json.load(f)
    env_val = os.getenv("GOOGLE_TOKEN_JSON")
    if env_val:
        return json.loads(env_val)
    return None


def _save_token(creds: Credentials):
    token_json = creds.to_json()
    try:
        with open(TOKEN_FILE, "w") as f:
            f.write(token_json)
    except Exception:
        logger.info("Cannot write token file, using env-only mode")


def get_credentials(
    credentials_json: dict | None = None,
    token_dict: dict | None = None,
    on_token_refresh=None,
) -> Credentials:
    """Resolve Google OAuth credentials.

    Zero-arg (or both dicts None) → the original single-tenant behavior:
    token.json / credentials.json files, GOOGLE_* env vars, and — as a last
    resort — the interactive InstalledAppFlow browser handshake.

    With `token_dict` supplied (per-org connector): headless ONLY. The files,
    env vars, and the interactive flow are NEVER touched (background workers
    have no browser). The supplied token is refreshed when needed and
    `on_token_refresh(new_token_dict)` is invoked on success so the caller can
    persist it back to the org's connector row. A refresh failure raises a
    clear, org-facing error (the fix is for an admin to reconnect Drive).
    """
    if token_dict is not None:
        return _credentials_from_dicts(credentials_json, token_dict, on_token_refresh)
    return _credentials_from_files()


def _credentials_from_dicts(
    credentials_json: dict, token_dict: dict | None, on_token_refresh,
) -> Credentials:
    creds = Credentials.from_authorized_user_info(token_dict, SCOPES) if token_dict else None
    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            raise RuntimeError(
                "Could not refresh the org's stored Google Drive token — the "
                "connection has likely been revoked or expired. Ask an org admin "
                "to reconnect Drive (re-save the Drive connector)."
            ) from e
        if on_token_refresh is not None:
            on_token_refresh(json.loads(creds.to_json()))
        return creds

    raise RuntimeError(
        "The org's stored Google Drive token is present but not usable (no "
        "refresh_token or already invalid). Ask an org admin to reconnect Drive "
        "(re-save the Drive connector)."
    )


def _credentials_from_files() -> Credentials:
    creds = None
    token_dict = _get_token_dict()
    if token_dict:
        creds = Credentials.from_authorized_user_info(token_dict, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_token(creds)
        else:
            cred_json = _get_credentials_json()
            if not cred_json:
                raise FileNotFoundError(
                    "Google credentials not found. Set credentials.json file or GOOGLE_CREDENTIALS_JSON env var."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                os.path.abspath(CREDENTIALS_FILE) if os.path.exists(CREDENTIALS_FILE) else None,
                SCOPES,
                client_config=cred_json if not os.path.exists(CREDENTIALS_FILE) else None,
            )
            creds = flow.run_local_server(port=0)
            _save_token(creds)
    return creds