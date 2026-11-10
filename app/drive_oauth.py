"""Self-serve Google Drive connector onboarding (OAuth web flow).

One deployment-level Google OAuth client ("Web application" type) is
configured via GOOGLE_OAUTH_CLIENT_ID / _CLIENT_SECRET / _REDIRECT_URI. An
org admin starts the flow in the UI:

  GET /api/v1/orgs/{org_id}/connectors/drive/oauth/start?folder_id=...
    → 200 {"authorize_url"} + an HttpOnly browser-binding cookie

Google redirects the browser to the org-less callback — no Authorization
header can arrive there, so the SERVER-SIDE PENDING ROW is the authority
(see DriveOAuthPending in app/database.py):

  GET /api/v1/connectors/drive/oauth/callback?code&state
    → looks up the pending row by the `state` NONCE (Google echoes it back),
      binds the returning browser via a hashed HttpOnly cookie,
      atomically consumes the row (single-use, replay-proof), revalidates
      the initiating user is STILL an org admin in the DB, exchanges the
      code with the row's decrypted PKCE code_verifier, stores the encrypted
      per-org connector, audits it, and 302s back to the frontend.

The opaque `state` string alone never authorizes anything — a signature or
guessable value is not enough: the row lookup + browser cookie + single-use
consume + DB role revalidation are all required.

The installed-app credentials.json flow (google_auth.py) is untouched; the
web client dict stored in the connector config is exactly the shape the
existing headless per-org Drive path consumes.
"""
import hashlib
import json
import logging
import secrets
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

# The admin must complete Google consent within this window; the pending row
# and the browser-binding cookie use the same lifetime.
STATE_TTL_SECONDS = 600

# One active OAuth flow per browser: starting a new flow OVERWRITES the
# browser-binding cookie, deliberately abandoning older in-progress flows
# in that browser (their callbacks fail the cookie check). Nothing is
# kept in sync server-side — abandoned rows simply expire and are lazily
# deleted by the callback path.
STATE_PURPOSE = "drive_oauth"


class OAuthStateError(ValueError):
    """Raised when an OAuth callback cannot be attributed to a legitimate
    pending flow. `reason` is a short, safe code for frontend redirects — it
    never contains the raw token, cookie, exception text, or config values."""

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


def _sha256(value: str) -> str:
    """Hex sha256 — used for the state nonce and the browser secret. The raw
    high-entropy values are never stored server-side."""
    return hashlib.sha256(value.encode()).hexdigest()


def new_nonce() -> str:
    """The opaque `state` value sent to Google: a high-entropy random token.
    Only its sha256 is persisted (lookup key); it is single-use via the
    pending row's consume."""
    return secrets.token_urlsafe(32)


def new_browser_secret() -> str:
    """A separate high-entropy secret, delivered to the initiating browser as
    an HttpOnly cookie scoped to the callback route. Its sha256 is persisted;
    the callback constant-time-compares the cookie against that hash."""
    return secrets.token_urlsafe(32)


def verify_browser_secret(cookie_value: str, stored_hash: str) -> bool:
    """Constant-time comparison of the cookie against the stored hash."""
    return secrets.compare_digest(_sha256(cookie_value), stored_hash)


def new_code_verifier() -> str:
    """PKCE code_verifier (RFC 7636): 43-128 chars from the unreserved set.
    Stored ENCRYPTED (connector-secret facilities); the S256 challenge goes
    on the authorize URL."""
    return secrets.token_urlsafe(48)  # ~64 chars, URL-safe alphabet


def code_challenge_s256(code_verifier: str) -> str:
    """RFC 7636 S256: BASE64URL(SHA256(ASCII(verifier))) without padding."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    import base64

    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def build_authorize_url(nonce: str, code_challenge: str) -> str:
    """Google's authorization endpoint with offline access (refresh_token
    forced via prompt=consent), the configured redirect URI, and PKCE S256."""
    from app import config

    params = {
        "response_type": "code",
        "client_id": config.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": config.GOOGLE_OAUTH_REDIRECT_URI,
        "scope": " ".join(config.SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": nonce,
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"


def exchange_code(code: str, credentials_web_dict: dict, code_verifier: str) -> dict:
    """Exchange an authorization code with Google, proving the client holds
    the PKCE verifier (PKCE works with google-auth-oauthlib 1.2.1's Flow:
    it forwards code_verifier to the token endpoint on fetch_token).
    Returns `creds.to_json()` (dict). Kept as a module function so tests can
    fake the network."""
    from google_auth_oauthlib.flow import Flow

    from app import config

    flow = Flow.from_client_config(
        {"web": credentials_web_dict},
        scopes=config.SCOPES,
        redirect_uri=config.GOOGLE_OAUTH_REDIRECT_URI,
        code_verifier=code_verifier,
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
