"""Authentication helpers.

Two modes are supported, controlled by config:
- Shared-secret HS256 JWTs (dev / fallback) when JWT_SECRET is set and AUTH0_ENABLED is false.
- Auth0 RS256 JWTs verified against the tenant's JWKS endpoint when AUTH0_ENABLED is true.

Both modes resolve a `user_id` (the JWT `sub` claim) used for multi-tenant isolation.
"""
import logging
import time
import httpx
from jose import jwt, JWTError
from app.config import (
    JWT_SECRET, JWT_ALGORITHM,
    AUTH0_ENABLED, AUTH0_DOMAIN, AUTH0_AUDIENCE, AUTH0_JWKS_CACHE_TTL,
)

logger = logging.getLogger(__name__)

_jwks_cache: dict = {"fetched_at": 0.0, "keys": []}


class AuthError(Exception):
    def __init__(self, detail: str, status_code: int = 401):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _jwks_url() -> str:
    return f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"


def _fetch_jwks() -> list[dict]:
    now = time.time()
    if _jwks_cache["keys"] and (now - _jwks_cache["fetched_at"]) < AUTH0_JWKS_CACHE_TTL:
        return _jwks_cache["keys"]
    try:
        resp = httpx.get(_jwks_url(), timeout=10.0)
        resp.raise_for_status()
    except Exception as e:
        raise AuthError(f"Unable to fetch Auth0 JWKS: {e}", 503)
    keys = resp.json().get("keys", [])
    _jwks_cache["fetched_at"] = now
    _jwks_cache["keys"] = keys
    return keys


def _verify_auth0_token(token: str) -> str:
    try:
        unverified_header = jwt.get_unverified_header(token)
    except JWTError:
        raise AuthError("Malformed token header")

    kid = unverified_header.get("kid")
    if not kid:
        raise AuthError("Token missing 'kid' header — not an Auth0-issued token")
    keys = _fetch_jwks()
    signing_key = next((k for k in keys if k.get("kid") == kid), None)
    if signing_key is None:
        raise AuthError("Signing key not found in JWKS", 401)

    try:
        payload = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=AUTH0_AUDIENCE if AUTH0_AUDIENCE else None,
            issuer=f"https://{AUTH0_DOMAIN}/",
        )
    except JWTError as e:
        raise AuthError(f"Invalid Auth0 token: {e}")

    user_id = payload.get("sub")
    if not user_id:
        raise AuthError("Token missing 'sub' claim")
    return str(user_id)


def _verify_shared_secret_token(token: str) -> str:
    if not JWT_SECRET:
        raise AuthError("No JWT_SECRET configured for shared-secret mode", 500)
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise AuthError("Invalid or expired token")
    user_id = payload.get("sub")
    if not user_id:
        raise AuthError("Token missing 'sub' claim")
    return str(user_id)


def verify_token(token: str) -> str:
    """Verify a bearer token and return the user_id.

    Auth0 is used when enabled; otherwise falls back to HS256 shared-secret mode.
    If neither is configured, the raw token is treated as the user_id (dev only).
    """
    if not token:
        raise AuthError("Empty token")
    if AUTH0_ENABLED:
        return _verify_auth0_token(token)
    if JWT_SECRET:
        return _verify_shared_secret_token(token)
    logger.warning("No auth provider configured — treating raw token as user_id (dev mode)")
    return token