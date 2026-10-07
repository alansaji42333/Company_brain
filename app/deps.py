"""FastAPI dependencies for the org-scoped auth context.

Replaces the old `get_user_id` dependency: instead of returning just the
verified `user_id`, it resolves the full AuthContext (user_id, org_id, role)
by verifying an actual membership. The `org_id` claim / X-Org-Id header is
only a hint — if no membership exists the request is rejected with 403.
"""
from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthContext, verify_token_payload, AuthError
from app.database import get_db
from app.orgs import resolve_auth_context


async def get_auth_context(
    authorization: str | None = Header(None),
    x_org_id: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
) -> AuthContext:
    """FastAPI dependency: verify the Bearer token and resolve the org context."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization Bearer token required")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = verify_token_payload(token)
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    user_id = str(payload.get("sub") or "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing 'sub' claim")

    org_candidate = _org_candidate(payload, x_org_id)
    try:
        return await resolve_auth_context(db, user_id, org_candidate)
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


def require_role(*roles):
    """Build a dependency enforcing that the caller holds one of `roles`."""
    async def _dep(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if ctx.role not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"Requires role(s): {', '.join(roles)}",
            )
        return ctx
    return _dep


async def resolve_ctx_from_token(
    db: AsyncSession,
    token: str,
    x_org_id: str | None = None,
    payload_org_hint: str | None = None,
) -> AuthContext:
    """Resolve an AuthContext from a raw token (used by root routes + WebSocket).

    `payload_org_hint` is an explicit org candidate supplied by the client that
    does not come from the verified token (e.g. an `org_id` field in the WS
    auth message). It is treated the same as an X-Org-Id header: only a hint.
    """
    payload = verify_token_payload(token)
    user_id = str(payload.get("sub") or "")
    if not user_id:
        raise AuthError("Token missing 'sub' claim")
    org_candidate = _org_candidate(payload, x_org_id or payload_org_hint)
    return await resolve_auth_context(db, user_id, org_candidate)


def _org_candidate(payload: dict, x_org_id: str | None) -> str | None:
    claimed = payload.get("org_id")
    if claimed:
        return str(claimed)
    if x_org_id and x_org_id.strip():
        return x_org_id.strip()
    return None