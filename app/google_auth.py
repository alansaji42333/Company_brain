import os
import json
import logging
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from app.config import SCOPES, CREDENTIALS_FILE, TOKEN_FILE

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


def get_credentials() -> Credentials:
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