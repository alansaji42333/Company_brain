"""API routers — versioned (/api/v1) and ingest (/api/ingest) endpoints."""
import logging
from fastapi import APIRouter, HTTPException, Depends, Request, Header
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.config import RATE_LIMIT, limiter
from app.auth import verify_token, AuthError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["v1"])
ingest_router = APIRouter(prefix="/api/ingest", tags=["ingest"])


# --- dependencies --------------------------------------------------------

async def get_user_id(authorization: str | None = Header(None)) -> str:
    """FastAPI dependency: verify the Bearer token and return the user_id."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization Bearer token required")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        return verify_token(token)
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


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
    user_id: str = Depends(get_user_id),
):
    from app.skill_synthesis import synthesize as run_synthesis
    summary = run_synthesis(user_id=user_id)
    return {"status": "ok", **summary}


@router.post("/synthesize/async", response_model=IngestResponse)
@limiter.limit(RATE_LIMIT)
async def synthesize_async(
    request: Request,
    user_id: str = Depends(get_user_id),
):
    from app.jobs import enqueue_synthesis
    pool = _require_redis(request)
    job_id = await enqueue_synthesis(pool, user_id=user_id)
    return IngestResponse(status="queued", job_id=job_id)


@router.get("/skills")
def list_skills(user_id: str = Depends(get_user_id)):
    from app.skill_store import list_skills as ls
    return {"skills": ls(user_id=user_id)}


@router.get("/skills/{skill_id}")
def get_skill(skill_id: str, user_id: str = Depends(get_user_id)):
    from app.skill_store import get_skill as gs
    skill = gs(skill_id, user_id=user_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@router.put("/skills/{skill_id}")
def update_skill(skill_id: str, req: UpdateSkillRequest, user_id: str = Depends(get_user_id)):
    from app.skill_store import save_skill
    save_skill(skill_id, req.content, user_id=user_id)
    return {"status": "ok"}


@router.post("/skills/{skill_id}/approve")
async def approve_skill(skill_id: str, user_id: str = Depends(get_user_id), db: AsyncSession = Depends(get_db)):
    from app.skill_store import approve_skill as approve
    from app import audit
    try:
        approve(skill_id, user_id=user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await audit.record(db, user_id, audit.ACTION_SKILL_APPROVED, payload={"skill_id": skill_id})
    await db.commit()
    return {"status": "ok"}


@router.post("/skills/{skill_id}/reject")
async def reject_skill(skill_id: str, user_id: str = Depends(get_user_id), db: AsyncSession = Depends(get_db)):
    from app.skill_store import reject_skill as reject
    from app import audit
    try:
        reject(skill_id, user_id=user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await audit.record(db, user_id, audit.ACTION_SKILL_REJECTED, payload={"skill_id": skill_id})
    await db.commit()
    return {"status": "ok"}


@router.post("/chat", response_model=ChatResponse)
@limiter.limit(RATE_LIMIT)
async def chat(
    request: Request,
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    from app.agent import send_message
    result = await send_message(req.conversation_id, req.message, db, user_id=user_id)
    return result


@router.post("/chat/confirm")
@limiter.limit(RATE_LIMIT)
async def chat_confirm(
    request: Request,
    req: ConfirmRequest,
    db: AsyncSession = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    from app.agent import confirm_action
    result = await confirm_action(str(req.conversation_id), req.approved, db, user_id=user_id)
    return result


@router.get("/conversations")
async def list_conversations(
    user_id: str = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import select
    from app.database import Conversation
    result = await db.execute(
        select(Conversation).where(Conversation.user_id == user_id).order_by(Conversation.updated_at.desc())
    )
    convs = result.scalars().all()
    return {"conversations": [{"id": c.id, "created_at": c.created_at, "updated_at": c.updated_at} for c in convs]}


@router.get("/audit")
async def list_audit(
    user_id: str = Depends(get_user_id),
    db: AsyncSession = Depends(get_db),
    limit: int = 100,
):
    """Return the most recent audit entries for the authenticated user."""
    from app.audit import list_for_user
    rows = await list_for_user(db, user_id, limit=limit)
    return {"audit": rows}


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
    user_id: str = Depends(get_user_id),
    folder_id: str | None = None,
):
    from app.jobs import enqueue_ingest_drive
    pool = _require_redis(request)
    job_id = await enqueue_ingest_drive(pool, user_id=user_id, folder_id=folder_id)
    return IngestJobResponse(job_id=job_id)


@ingest_router.post("/slack", response_model=IngestJobResponse)
@limiter.limit(RATE_LIMIT)
async def ingest_slack_async(
    request: Request,
    user_id: str = Depends(get_user_id),
):
    from app.jobs import enqueue_ingest_slack
    pool = _require_redis(request)
    job_id = await enqueue_ingest_slack(pool, user_id=user_id)
    return IngestJobResponse(job_id=job_id)


@ingest_router.get("/status/{job_id}", response_model=IngestStatusResponse)
@limiter.limit(RATE_LIMIT)
async def ingest_status(
    request: Request,
    job_id: str,
    user_id: str = Depends(get_user_id),
):
    from app.jobs import get_job_status
    pool = _require_redis(request)
    status = await get_job_status(pool, job_id, user_id=user_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return IngestStatusResponse(**status)