"""API routers — versioned (/api/v1) and ingest (/api/ingest) endpoints.

All data endpoints are org-scoped: the auth context dependency resolves the
caller's (user_id, org_id, role) by verifying a real membership, and every
query filters on that org_id. Role gates are applied with `require_role`.
"""
import logging
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.config import RATE_LIMIT, limiter
from app.auth import AuthContext
from app.deps import get_auth_context, require_role
from app.orgs import (
    ROLE_ADMIN, REVIEW_ROLES, get_organization, create_org, list_members,
    add_member, set_member_role, remove_member,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["v1"])
ingest_router = APIRouter(prefix="/api/ingest", tags=["ingest"])


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


# --- routes --------------------------------------------------------------

@router.post("/synthesize")
@limiter.limit(RATE_LIMIT)
def synthesize(
    request: Request,
    ctx: AuthContext = Depends(require_role(*REVIEW_ROLES)),
):
    from app.skill_synthesis import synthesize as run_synthesis
    summary = run_synthesis(org_id=ctx.org_id)
    return {"status": "ok", "org_id": ctx.org_id, **summary}


@router.post("/synthesize/async", response_model=IngestResponse)
@limiter.limit(RATE_LIMIT)
async def synthesize_async(
    request: Request,
    ctx: AuthContext = Depends(require_role(*REVIEW_ROLES)),
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
    ctx: AuthContext = Depends(require_role(*REVIEW_ROLES)),
):
    from app.skill_store import save_skill
    save_skill(org_id=ctx.org_id, skill_id=skill_id, body_content=req.content)
    return {"status": "ok"}


@router.post("/skills/{skill_id}/approve")
async def approve_skill(
    skill_id: str,
    ctx: AuthContext = Depends(require_role(*REVIEW_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    from app.skill_store import approve_skill as approve
    from app import audit
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
    ctx: AuthContext = Depends(require_role(*REVIEW_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    from app.skill_store import reject_skill as reject
    from app import audit
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
    ctx: AuthContext = Depends(require_role(ROLE_ADMIN)),
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
    ctx: AuthContext = Depends(require_role(ROLE_ADMIN)),
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
    ctx: AuthContext = Depends(require_role(ROLE_ADMIN)),
):
    """Change a member's role. The last admin cannot be demoted."""
    _require_member_of(ctx, org_id)
    if req.role not in (ROLE_ADMIN, "reviewer", "member"):
        raise HTTPException(status_code=400, detail="Invalid role")
    try:
        member = await set_member_role(db, org_id, user_id, req.role)
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
    ctx: AuthContext = Depends(require_role(ROLE_ADMIN)),
):
    """Remove a member from an org. The last admin cannot be removed."""
    _require_member_of(ctx, org_id)
    try:
        await remove_member(db, org_id, user_id)
    except ValueError as e:
        detail = str(e)
        status = 404 if "not found" in detail else 409
        raise HTTPException(status_code=status, detail=detail)
    await db.commit()
    return {"org_id": org_id, "user_id": user_id, "removed": True}


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
    ctx: AuthContext = Depends(require_role(*REVIEW_ROLES)),
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
    ctx: AuthContext = Depends(require_role(*REVIEW_ROLES)),
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
