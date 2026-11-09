"""Self-serve Google Drive connector onboarding (OAuth web flow).

One deployment-level Google OAuth client ("Web application" type) is
configured via GOOGLE_OAUTH_CLIENT_ID / _CLIENT_SECRET / _REDIRECT_URI. An
org admin starts the flow in the UI:

  GET /api/v1/orgs/{org_id}/connectors/drive/oauth/start?folder_id=...
    → 302 to Google's consent screen (access_type=offline, prompt=consent so
      a refresh_token is always issued for the org).

Google redirects the browser to the callback — no Authorization header can
arrive there, so the HMAC-signed `state` (JWT, 10-minute expiry) is the
authorization:

  GET /api/v1/orgs/{org_id}/connectors/drive/oauth/callback?code&state
    → exchanges the code, stores the encrypted per-org connector, audits it,
      and 302s back to the frontend's connectors page.

The installed-app credentials.json flow (google_auth.py) is untouched; the
web client dict stored in the connector config is exactly the shape the
existing headless per-org Drive path consumes.
"""
import json
import logging
from urllib.parse import urlencode

from jose import jwt as jose_jwt
from jose.exceptions import ExpiredSignatureError as _JoseExpiredSignature
from jose.exceptions import JWTError as _JoseJWTError

logger = logging.getLogger(__name__)

# State token lifetime (seconds) — the admin must complete Google consent
# within this window.
STATE_TTL_SECONDS = 600

STATE_PURPOSE = "drive_oauth"


class OAuthStateError(ValueError):
    """Raised when an OAuth callback's signed state cannot be trusted.

    `reason` is a short, safe code for frontend redirects — it never contains
    the raw token, exception text, or config values.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def oauth_configured() -> bool:
    """True when the deployment-level OAuth web client is fully configured.
    Read dynamically so patched/env-driven config takes effect per call."""
    from app import config

    return bool(
        config.GOOGLE_OAUTH_CLIENT_ID
        and config.GOOGLE_OAUTH_CLIENT_SECRET
        and config.GOOGLE_OAUTH_REDIRECT_URI
    )


def build_authorize_url(state: str) -> str:
    """Google's authorization endpoint with offline access (refresh_token
    forced via prompt=consent) and the configured redirect URI."""
    from app import config

    params = {
        "response_type": "code",
        "client_id": config.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": config.GOOGLE_OAUTH_REDIRECT_URI,
        "scope": " ".join(config.SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"


def make_state(org_id: str, user_id: str, folder_id: str) -> str:
    """Sign the callback authorization: org_id + user_id + folder_id, 10-min
    expiry, HS256 with JWT_SECRET. Refuses to start without a signing secret."""
    from datetime import datetime, timedelta, timezone

    from app import config

    if not config.JWT_SECRET:
        raise RuntimeError(
            "Drive OAuth onboarding requires a signing secret: set JWT_SECRET"
        )
    now = datetime.now(timezone.utc)
    return jose_jwt.encode(
        {
            "purpose": STATE_PURPOSE,
            "org_id": org_id,
            "user_id": user_id,
            "folder_id": folder_id,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=STATE_TTL_SECONDS)).timestamp()),
        },
        config.JWT_SECRET,
        algorithm="HS256",
    )


def verify_state(token: str, path_org_id: str) -> dict:
    """Verify signature, expiry, purpose, and that the callback's path org
    matches the signed org. Returns the claims or raises OAuthStateError."""
    from app import config

    try:
        claims = jose_jwt.decode(
            token,
            config.JWT_SECRET,
            algorithms=["HS256"],
            options={"require": ["exp", "purpose", "org_id", "user_id", "folder_id"]},
        )
    except _JoseExpiredSignature:
        raise OAuthStateError("expired_state") from None
    except _JoseJWTError:
        raise OAuthStateError("invalid_state") from None

    if claims.get("purpose") != STATE_PURPOSE:
        raise OAuthStateError("invalid_state")
    if claims.get("org_id") != path_org_id:
        raise OAuthStateError("org_mismatch")
    return claims


def exchange_code(code: str, credentials_web_dict: dict) -> dict:
    """Exchange an authorization code with Google. Returns `creds.to_json()`
    (dict). Kept as a module function so tests can fake the network."""
    from google_auth_oauthlib.flow import Flow

    from app import config

    flow = Flow.from_client_config(
        {"web": credentials_web_dict}, scopes=config.SCOPES,
        redirect_uri=config.GOOGLE_OAUTH_REDIRECT_URI,
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    return json.loads(creds.to_json())


def web_client_dict() -> dict:
    """The deployment-level OAuth web client as a dict for exchange + the
    stored connector config (the {web: {...}} shape Google issues)."""
    from app import config

    return {
        "web": {
            "client_id": config.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": config.GOOGLE_OAUTH_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [config.GOOGLE_OAUTH_REDIRECT_URI],
        }
    }
