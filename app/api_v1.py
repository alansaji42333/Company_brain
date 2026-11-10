"""API routers — versioned (/api/v1) and ingest (/api/ingest) endpoints.

All data endpoints are org-scoped: the auth context dependency resolves the
caller's (user_id, org_id, role) by verifying a real membership, and every
query filters on that org_id. Role gates are applied with `require_role`.
"""
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthContext
from app.config import RATE_LIMIT, limiter
from app.database import get_db
from app.deps import get_auth_context, require_role
from app.orgs import (
    REVIEW_ROLES,
    ROLE_ADMIN,
    add_member,
    create_org,
    get_organization,
    list_members,
    list_user_orgs,
    remove_member,
    set_member_role,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["v1"])
ingest_router = APIRouter(prefix="/api/ingest", tags=["ingest"])

# Pre-built role-gate dependencies (avoids function calls in argument defaults).
require_reviewer = require_role(*REVIEW_ROLES)
require_admin = require_role(ROLE_ADMIN)


# --- models ---------------------------------------------------------------

class Source(BaseModel):
    name: str = ""
    url: str = ""
    type: str = ""


class ChatResponse(BaseModel):
    type: str
    conversation_id: str | None = None
    answer: str | None = None
    sources: list[Source] = []
    description: str | None = None
    tool_name: str | None = None
    tool_input: dict | None = None
    explanation: str | None = None


class ConfirmResponse(BaseModel):
    type: str
    conversation_id: str | None = None
    answer: str | None = None
    sources: list[Source] = []
    error: str | None = None


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str


class ConfirmRequest(BaseModel):
    conversation_id: str
    approved: bool


class UpdateSkillRequest(BaseModel):
    content: str


class CreateOrgRequest(BaseModel):
    name: str


class AddMemberRequest(BaseModel):
    user_id: str
    role: str = "member"


class UpdateMemberRequest(BaseModel):
    role: str


class IngestResponse(BaseModel):
    status: str
    files_processed: int | None = None
    chunks_stored: int | None = None
    job_id: str | None = None


class IngestJobResponse(BaseModel):
    job_id: str
    status: str = "queued"


class IngestStatusResponse(BaseModel):
    job_id: str
    status: str
    result: dict | None = None


class SlackConnectorRequest(BaseModel):
    bot_token: str
    channel_ids: list[str]


class DriveConnectorRequest(BaseModel):
    folder_id: str
    credentials_json: dict
    token_json: dict


# --- routes --------------------------------------------------------------

@router.post("/synthesize")
@limiter.limit(RATE_LIMIT)
def synthesize(
    request: Request,
    ctx: AuthContext = Depends(require_reviewer),
):
    from app.skill_synthesis import synthesize as run_synthesis
    summary = run_synthesis(org_id=ctx.org_id)
    return {"status": "ok", "org_id": ctx.org_id, **summary}


@router.post("/synthesize/async", response_model=IngestResponse)
@limiter.limit(RATE_LIMIT)
async def synthesize_async(
    request: Request,
    ctx: AuthContext = Depends(require_reviewer),
):
    from app.jobs import enqueue_synthesis
    pool = _require_redis(request)
    job_id = await enqueue_synthesis(pool, user_id=ctx.user_id, org_id=ctx.org_id)
    return IngestResponse(status="queued", job_id=job_id)


@router.get("/skills")
def list_skills(ctx: AuthContext = Depends(get_auth_context)):
    from app.skill_store import list_skills as ls
    only_approved = ctx.role not in REVIEW_ROLES
    return {"skills": ls(org_id=ctx.org_id, only_approved=only_approved)}


@router.get("/skills/{skill_id}")
def get_skill(skill_id: str, ctx: AuthContext = Depends(get_auth_context)):
    from app.skill_store import get_skill as gs
    only_approved = ctx.role not in REVIEW_ROLES
    skill = gs(org_id=ctx.org_id, skill_id=skill_id, only_approved=only_approved)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@router.put("/skills/{skill_id}")
def update_skill(
    skill_id: str,
    req: UpdateSkillRequest,
    ctx: AuthContext = Depends(require_reviewer),
):
    from app.skill_store import save_skill
    save_skill(org_id=ctx.org_id, skill_id=skill_id, body_content=req.content)
    return {"status": "ok"}


@router.post("/skills/{skill_id}/approve")
async def approve_skill(
    skill_id: str,
    ctx: AuthContext = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    from app import audit
    from app.skill_store import approve_skill as approve
    try:
        approve(org_id=ctx.org_id, skill_id=skill_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await audit.record(db, ctx.org_id, ctx.user_id, audit.ACTION_SKILL_APPROVED, payload={"skill_id": skill_id})
    await db.commit()
    return {"status": "ok"}


@router.post("/skills/{skill_id}/reject")
async def reject_skill(
    skill_id: str,
    ctx: AuthContext = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    from app import audit
    from app.skill_store import reject_skill as reject
    try:
        reject(org_id=ctx.org_id, skill_id=skill_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await audit.record(db, ctx.org_id, ctx.user_id, audit.ACTION_SKILL_REJECTED, payload={"skill_id": skill_id})
    await db.commit()
    return {"status": "ok"}


@router.post("/chat", response_model=ChatResponse)
@limiter.limit(RATE_LIMIT)
async def chat(
    request: Request,
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    from app.agent import send_message
    result = await send_message(
        req.conversation_id, req.message, db, user_id=ctx.user_id, org_id=ctx.org_id,
    )
    return result


@router.post("/chat/confirm")
@limiter.limit(RATE_LIMIT)
async def chat_confirm(
    request: Request,
    req: ConfirmRequest,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    from app.agent import confirm_action
    result = await confirm_action(
        str(req.conversation_id), req.approved, db, user_id=ctx.user_id, org_id=ctx.org_id,
    )
    return result


@router.get("/conversations")
async def list_conversations(
    ctx: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """List the caller's own conversations within their org."""
    from sqlalchemy import select

    from app.database import Conversation
    result = await db.execute(
        select(Conversation).where(
            Conversation.user_id == ctx.user_id,
            Conversation.org_id == ctx.org_id,
        ).order_by(Conversation.updated_at.desc())
    )
    convs = result.scalars().all()
    return {
        "conversations": [
            {"id": c.id, "org_id": c.org_id, "created_at": c.created_at, "updated_at": c.updated_at}
            for c in convs
        ]
    }


@router.get("/audit")
async def list_audit(
    ctx: AuthContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    limit: int = 100,
):
    """Return the most recent audit entries for the org (admins only)."""
    from app.audit import list_for_org
    rows = await list_for_org(db, ctx.org_id, limit=limit)
    return {"audit": rows}


# --- organizations & membership ------------------------------------------

@router.post("/orgs")
async def create_organization(
    req: CreateOrgRequest,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    """Create a new organization; the caller becomes its admin."""
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Organization name is required")
    try:
        org = await create_org(db, name=name, owner_user_id=ctx.user_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    await db.commit()
    return {
        "id": org.id,
        "name": org.name,
        "slug": org.slug,
        "role": ROLE_ADMIN,
    }


@router.get("/orgs")
async def list_my_orgs(
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    """Orgs the caller belongs to (for the client's org switcher).

    Switching is a client hint (`X-Org-Id`): every request still resolves
    and verifies the membership server-side.
    """
    return {"orgs": await list_user_orgs(db, ctx.user_id)}


@router.get("/orgs/me")
async def get_my_org(
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    """Return the caller's active org, their role, and org members."""
    org = await get_organization(db, ctx.org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    members = await list_members(db, ctx.org_id)
    return {
        "id": org.id,
        "name": org.name,
        "slug": org.slug,
        "role": ctx.role,
        "user_id": ctx.user_id,
        "members": members,
    }


@router.get("/orgs/{org_id}/members")
async def get_members(
    org_id: str,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    """List members of an org the caller belongs to (any role)."""
    _require_member_of(ctx, org_id)
    org = await get_organization(db, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return {"org_id": org_id, "name": org.name, "members": await list_members(db, org_id)}


@router.post("/orgs/{org_id}/members")
async def add_org_member(
    org_id: str,
    req: AddMemberRequest,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_admin),
):
    """Add or update a member's role (admins of that org only)."""
    _require_member_of(ctx, org_id)
    if req.role not in (ROLE_ADMIN, "reviewer", "member"):
        raise HTTPException(status_code=400, detail="Invalid role")
    if not req.user_id.strip():
        raise HTTPException(status_code=400, detail="user_id is required")
    try:
        member = await add_member(db, org_id, req.user_id.strip(), req.role)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await db.commit()
    return {
        "org_id": member.org_id,
        "user_id": member.user_id,
        "role": member.role,
        "created_at": member.created_at,
    }


@router.patch("/orgs/{org_id}/members")
async def update_org_member(
    org_id: str,
    req: UpdateMemberRequest,
    user_id: str,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_admin),
):
    """Change a member's role. The last admin cannot be demoted."""
    _require_member_of(ctx, org_id)
    if req.role not in (ROLE_ADMIN, "reviewer", "member"):
        raise HTTPException(status_code=400, detail="Invalid role")
    try:
        member = await set_member_role(db, org_id, user_id, req.role, actor_user_id=ctx.user_id)
    except ValueError as e:
        detail = str(e)
        status = 404 if "not found" in detail else 409
        raise HTTPException(status_code=status, detail=detail)
    await db.commit()
    return {"org_id": org_id, "user_id": user_id, "role": member.role}


@router.delete("/orgs/{org_id}/members/{user_id}")
async def delete_org_member(
    org_id: str,
    user_id: str,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_admin),
):
    """Remove a member from an org. The last admin cannot be removed."""
    _require_member_of(ctx, org_id)
    try:
        await remove_member(db, org_id, user_id, actor_user_id=ctx.user_id)
    except ValueError as e:
        detail = str(e)
        status = 404 if "not found" in detail else 409
        raise HTTPException(status_code=status, detail=detail)
    await db.commit()
    return {"org_id": org_id, "user_id": user_id, "removed": True}


# --- connectors (per-org credential onboarding) -----------------------------

@router.get("/orgs/{org_id}/connectors")
async def list_org_connectors(
    org_id: str,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(get_auth_context),
):
    """Non-secret view of the org's configured connectors (any member)."""
    _require_member_of(ctx, org_id)
    from app import connectors
    org = await get_organization(db, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return {"org_id": org_id, "connectors": await connectors.list_connectors_public(db, org_id)}


@router.put("/orgs/{org_id}/connectors/slack")
async def put_slack_connector(
    org_id: str,
    req: SlackConnectorRequest,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_admin),
):
    """Store the org's Slack connector credentials (encrypted at rest)."""
    _require_member_of(ctx, org_id)
    from app import audit, connectors
    org = await get_organization(db, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    config = req.model_dump()
    try:
        await connectors.set_connector(db, org_id, "slack", config, ctx.user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await db.commit()
    # Audit records THAT a connector was set (actor + source) — never config.
    await audit.record(db, org_id, ctx.user_id, audit.ACTION_CONNECTOR_SET, payload={"source": "slack"})
    await db.commit()
    return {"org_id": org_id, "source": "slack", "configured": True}


@router.put("/orgs/{org_id}/connectors/drive")
async def put_drive_connector(
    org_id: str,
    req: DriveConnectorRequest,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_admin),
):
    """Store the org's Google Drive connector credentials (encrypted at rest)."""
    _require_member_of(ctx, org_id)
    from app import audit, connectors
    org = await get_organization(db, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    config = req.model_dump()
    try:
        await connectors.set_connector(db, org_id, "drive", config, ctx.user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await db.commit()
    await audit.record(db, org_id, ctx.user_id, audit.ACTION_CONNECTOR_SET, payload={"source": "drive"})
    await db.commit()
    return {"org_id": org_id, "source": "drive", "configured": True}


# --- Drive OAuth self-serve onboarding --------------------------------------
# The browser flow for org admins: start → Google consent → callback.
# The callback is ORG-LESS (one deployment-level GOOGLE_OAUTH_REDIRECT_URI
# can then serve every org) and carries no Authorization header — the
# SERVER-SIDE pending row (DriveOAuthPending) is the authority: browser-
# binding cookie + single-use atomic consume + DB role revalidation at
# callback time. app/drive_oauth.py holds the crypto and the token exchange;
# app/connectors.py the encrypted storage.

_CONNECTORS_PAGE = "/connectors"  # frontend route where the UI shows result

# The browser-binding cookie lives ONLY on the callback route: Google's
# top-level redirect back carries it (SameSite=Lax) and no other request
# can ever see it.
_DRIVE_COOKIE_PATH = "/api/v1/connectors/drive/oauth/callback"
_DRIVE_COOKIE_NAME = "drive_oauth_browser"


def _drive_oauth_unavailable_error() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=(
            "Drive OAuth onboarding is disabled: set GOOGLE_OAUTH_CLIENT_ID, "
            "GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REDIRECT_URI to enable it"
        ),
    )


@router.get("/orgs/{org_id}/connectors/drive/oauth/start")
async def start_drive_oauth(
    org_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    folder_id: str | None = None,
    ctx: AuthContext = Depends(require_admin),
):
    """Begin the Drive OAuth flow. Returns JSON: {"authorize_url"} plus an
    HttpOnly browser-binding cookie scoped to the callback route.

    The UI calls this through the authenticated api client (it sends the
    Bearer token) and then navigates the browser to authorize_url. Same
    admin + membership checks as the manual PUT endpoint; `folder_id` ends
    up in the pending row and later in the stored connector.
    """
    from fastapi.responses import JSONResponse
    from sqlalchemy import delete as sa_delete

    from app import connectors, drive_oauth
    from app.database import DriveOAuthPending

    _require_member_of(ctx, org_id)
    org = await get_organization(db, org_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not drive_oauth.oauth_configured():
        raise _drive_oauth_unavailable_error()
    if not folder_id or not folder_id.strip():
        raise HTTPException(status_code=400, detail="folder_id is required to configure the Drive connector")

    now = int(time.time())
    expires_at = now + drive_oauth.STATE_TTL_SECONDS
    nonce = drive_oauth.new_nonce()
    browser_secret = drive_oauth.new_browser_secret()
    code_verifier = drive_oauth.new_code_verifier()

    # One active flow per BROWSER: starting a new flow OVERWRITES the cookie
    # (older in-progress flows in this browser then fail the cookie check and
    # are abandoned deliberately). Their rows simply expire and are picked up
    # by the lazy expiry delete below.
    db.add(
        DriveOAuthPending(
            nonce_hash=drive_oauth._sha256(nonce),
            org_id=org_id,
            user_id=ctx.user_id,
            folder_id=folder_id.strip(),
            browser_secret_hash=drive_oauth._sha256(browser_secret),
            code_verifier_encrypted=connectors.encrypt_secret_string(code_verifier),
            created_at=now,
            expires_at=expires_at,
            consumed_at=None,
        )
    )
    # Lazy table hygiene: drop already-expired rows (never the one above —
    # its expires_at is in the future).
    await db.execute(sa_delete(DriveOAuthPending).where(DriveOAuthPending.expires_at < now))
    await db.commit()

    authorize_url = drive_oauth.build_authorize_url(nonce, drive_oauth.code_challenge_s256(code_verifier))
    response = JSONResponse({"authorize_url": authorize_url})
    response.set_cookie(
        key=_DRIVE_COOKIE_NAME,
        value=browser_secret,
        max_age=drive_oauth.STATE_TTL_SECONDS,
        path=_DRIVE_COOKIE_PATH,
        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
        httponly=True,
        samesite="lax",  # Google's top-level redirect back must carry it
    )
    logger.info("Drive OAuth started for org %s user %s", org_id, ctx.user_id)
    return response


@router.get("/connectors/drive/oauth/callback")
async def drive_oauth_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
    code: str | None = None,
    state: str | None = None,
):
    """Finish the Drive OAuth flow (org-less route — the org comes ONLY from
    the consumed pending row, never from the URL).

    No Authorization header exists here (Google's redirect) — authorization
    is the server-side pending row bound to this browser:
      1. row lookup by the state nonce (+ expiry), unknown/expired rejected;
      2. browser-binding cookie checked (constant-time) BEFORE any consume
         or token exchange;
      3. single-use atomic consume (conditional UPDATE, zero rows = reject);
      4. the initiating user must STILL be an org admin of the row's org;
      5. code exchange with the row's decrypted PKCE verifier;
      6. encrypted connector storage + audit, 302 back to the frontend with
         only short safe reason codes.
    The binding cookie is cleared on EVERY terminal outcome.
    """
    from fastapi.responses import RedirectResponse
    from sqlalchemy import select
    from sqlalchemy import update as sa_update

    from app import audit, connectors, drive_oauth, orgs
    from app.config import FRONTEND_URL
    from app.database import DriveOAuthPending

    if not drive_oauth.oauth_configured():
        raise _drive_oauth_unavailable_error()

    def _redirect(url: str) -> RedirectResponse:
        response = RedirectResponse(url, status_code=302)
        response.delete_cookie(_DRIVE_COOKIE_NAME, path=_DRIVE_COOKIE_PATH)
        return response

    def _fail(reason: str) -> RedirectResponse:
        return _redirect(f"{FRONTEND_URL}{_CONNECTORS_PAGE}?drive=error&reason={reason}")

    if not code or not state:
        return _fail("missing_params")

    now = int(time.time())
    row = await db.scalar(select(DriveOAuthPending).where(DriveOAuthPending.nonce_hash == drive_oauth._sha256(state)))
    if row is None or row.expires_at < now:
        # Lazy hygiene: drop the expired row if we found one. No exchange,
        # nothing stored, no cookie check needed (row is not trustworthy).
        if row is not None:
            await db.delete(row)
            await db.commit()
        return _fail("unknown_state" if row is None else "expired_state")

    # Initiator-browser binding FIRST (before consume/exchange): the cookie
    # must carry the browser secret issued at start. Constant-time compare.
    cookie_value = request.cookies.get(_DRIVE_COOKIE_NAME)
    if not cookie_value or not drive_oauth.verify_browser_secret(cookie_value, row.browser_secret_hash):
        logger.warning("Drive OAuth browser check failed for org %s (reason=browser_mismatch)", row.org_id)
        return _fail("browser_mismatch")

    # Single-use consume: ONE conditional UPDATE that only fires on a fresh
    # (unconsumed, unexpired) row; zero rows affected = already consumed
    # (replay) or expired. ATOMIC against concurrent callbacks: only one of
    # them flips consumed_at, the other sees zero rows. Committed
    # immediately — even a later exchange failure cannot resurrect the flow.
    consume_result = await db.execute(
        sa_update(DriveOAuthPending)
        .where(
            DriveOAuthPending.id == row.id,
            DriveOAuthPending.consumed_at.is_(None),
            DriveOAuthPending.expires_at >= now,
        )
        .values(consumed_at=now)
    )
    if consume_result.rowcount == 0:
        return _fail("consumed_state")
    await db.commit()

    # Authorization revalidated AT CALLBACK TIME from the DB: the initiating
    # user must still be an org admin of the row's org.
    membership = await orgs.get_membership(db, row.org_id, row.user_id)
    if membership is None or membership.role != orgs.ROLE_ADMIN:
        logger.warning("Drive OAuth callback revoked: user %s no longer admin of org %s", row.user_id, row.org_id)
        return _fail("revoked")

    try:
        # PKCE: the encrypted verifier decrypts ONLY here (in memory).
        code_verifier = connectors.decrypt_secret_string(row.code_verifier_encrypted)
        token_json = drive_oauth.exchange_code(code, drive_oauth.web_client_dict()["web"], code_verifier)
    except Exception:
        # Never log raw exception text (token URLs, auth codes etc.) or the
        # verifier — short safe reason only.
        logger.error("Drive OAuth code exchange failed for org %s", row.org_id)
        return _fail("exchange_failed")

    if not token_json.get("refresh_token"):
        # Offline access must yield a refresh_token; prompt=consent forces it.
        # A token without one cannot keep working long-term — save nothing.
        return _fail("no_refresh_token")

    config = {
        "folder_id": row.folder_id,
        "credentials_json": drive_oauth.web_client_dict()["web"],
        "token_json": token_json,
    }
    try:
        await connectors.set_connector(db, row.org_id, "drive", config, row.user_id)
    except ValueError as e:
        # Safe: config-shape rejection only; the message never contains the
        # token material (see app.connectors._validate).
        logger.error("Drive OAuth callback rejected config for org %s: %s", row.org_id, e)
        return _fail("invalid_config")
    await db.commit()
    await audit.record(
        db, row.org_id, row.user_id, audit.ACTION_CONNECTOR_SET, payload={"source": "drive", "via": "oauth"}
    )
    await db.commit()
    logger.info("Drive connector stored via OAuth for org %s user %s", row.org_id, row.user_id)
    return _redirect(f"{FRONTEND_URL}{_CONNECTORS_PAGE}?drive=connected")


@router.delete("/orgs/{org_id}/connectors/{source}")
async def delete_org_connector(
    org_id: str,
    source: str,
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(require_admin),
):
    """Delete the org's connector config for `source`.

    After deletion the org is UNCONFIGURED. Its ingest jobs stop before any
    source/vector work — env/file credentials apply only if the org is still
    the explicitly bound LEGACY_CONNECTOR_ORG_ID (see app.connectors).
    """
    _require_member_of(ctx, org_id)
    from app import audit, connectors
    try:
        removed = await connectors.delete_connector(db, org_id, source)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not removed:
        raise HTTPException(status_code=404, detail=f"No {source} connector configured")
    await db.commit()
    await audit.record(db, org_id, ctx.user_id, audit.ACTION_CONNECTOR_DELETED, payload={"source": source})
    await db.commit()
    return {"org_id": org_id, "source": source, "removed": True}


def _require_member_of(ctx: AuthContext, org_id: str):
    if ctx.org_id != org_id:
        raise HTTPException(status_code=403, detail="Not a member of this organization")


# --- async ingestion API (/api/ingest) -----------------------------------
# These endpoints enqueue background jobs on the shared ARQ pool stored on
# app.state.redis (created in server.py's lifespan) and return a job_id
# immediately. Poll GET /api/ingest/status/{job_id} for completion.

def _require_redis(request: Request):
    pool = getattr(request.app.state, "redis", None)
    if pool is None:
        raise HTTPException(
            status_code=503,
            detail="Background job queue unavailable (Redis not connected)",
        )
    return pool


@ingest_router.post("/drive", response_model=IngestJobResponse)
@limiter.limit(RATE_LIMIT)
async def ingest_drive_async(
    request: Request,
    ctx: AuthContext = Depends(require_reviewer),
    folder_id: str | None = None,
):
    from app.jobs import enqueue_ingest_drive
    pool = _require_redis(request)
    job_id = await enqueue_ingest_drive(pool, user_id=ctx.user_id, org_id=ctx.org_id, folder_id=folder_id)
    return IngestJobResponse(job_id=job_id)


@ingest_router.post("/slack", response_model=IngestJobResponse)
@limiter.limit(RATE_LIMIT)
async def ingest_slack_async(
    request: Request,
    ctx: AuthContext = Depends(require_reviewer),
):
    from app.jobs import enqueue_ingest_slack
    pool = _require_redis(request)
    job_id = await enqueue_ingest_slack(pool, user_id=ctx.user_id, org_id=ctx.org_id)
    return IngestJobResponse(job_id=job_id)


@ingest_router.get("/status/{job_id}", response_model=IngestStatusResponse)
@limiter.limit(RATE_LIMIT)
async def ingest_status(
    request: Request,
    job_id: str,
    ctx: AuthContext = Depends(get_auth_context),
):
    from app.jobs import get_job_status
    pool = _require_redis(request)
    status = await get_job_status(pool, job_id, user_id=ctx.user_id, org_id=ctx.org_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return IngestStatusResponse(**status)
