import os
import logging
import structlog
from fastapi import FastAPI, HTTPException, Depends, Header, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from jose import jwt, JWTError
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from prometheus_fastapi_instrumentator import Instrumentator
from app.database import get_db
from app.config import JWT_SECRET, JWT_ALGORITHM, CORS_ORIGINS, RATE_LIMIT, validate_config

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
slog = structlog.get_logger()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="Company Brain", version="1.0.0")
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})


app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in CORS_ORIGINS.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

Instrumentator().instrument(app).expose(app, endpoint="/metrics")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


def get_user_id(authorization: str | None = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization Bearer token required")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty token")
    if JWT_SECRET:
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
            user_id = payload.get("sub")
            if not user_id:
                raise HTTPException(status_code=401, detail="Token missing 'sub' claim")
            return str(user_id)
        except JWTError:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
    else:
        return token


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


def _read_static(filename: str) -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", filename)
    with open(path) as f:
        return f.read()


@app.on_event("startup")
async def on_startup():
    validate_config()
    from app.embeddings import _get_ef
    _get_ef()
    slog.info("startup_complete")


@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    try:
        await db.execute(text("SELECT 1"))
        return {"status": "healthy"}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unhealthy"})


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(_read_static("index.html"))


@app.get("/skills-page", response_class=HTMLResponse)
def skills_page():
    return HTMLResponse(_read_static("skills.html"))


@app.post("/ingest")
@limiter.limit(RATE_LIMIT)
def ingest(request: Request, folder_id: str | None = None, user_id: str = Depends(get_user_id)):
    from app.drive_ingest import ingest_drive_folder
    from app.chunking import chunk_documents
    from app.vectorstore import add_chunks

    documents = ingest_drive_folder(folder_id, user_id=user_id)
    chunks = chunk_documents(documents, user_id=user_id)
    add_chunks(chunks)

    return IngestResponse(status="ok", files_processed=len(documents), chunks_stored=len(chunks))


@app.post("/ingest/slack")
@limiter.limit(RATE_LIMIT)
def ingest_slack(request: Request, user_id: str = Depends(get_user_id)):
    from app.slack_ingest import ingest_slack as run_ingest
    from app.vectorstore import add_chunks

    chunks = run_ingest(user_id=user_id)
    add_chunks(chunks)

    return IngestResponse(status="ok", chunks_stored=len(chunks))


@app.post("/synthesize")
@limiter.limit(RATE_LIMIT)
def synthesize(request: Request, user_id: str = Depends(get_user_id)):
    from app.skill_synthesis import synthesize as run_synthesis

    summary = run_synthesis(user_id=user_id)
    return {"status": "ok", **summary}


@app.get("/skills")
def list_skills(user_id: str = Depends(get_user_id)):
    from app.skill_store import list_skills as ls
    return {"skills": ls(user_id=user_id)}


@app.get("/skills/{skill_id}")
def get_skill(skill_id: str, user_id: str = Depends(get_user_id)):
    from app.skill_store import get_skill as gs
    skill = gs(skill_id, user_id=user_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@app.put("/skills/{skill_id}")
def update_skill(skill_id: str, req: UpdateSkillRequest, user_id: str = Depends(get_user_id)):
    from app.skill_store import save_skill
    save_skill(skill_id, req.content, user_id=user_id)
    return {"status": "ok"}


@app.post("/skills/{skill_id}/approve")
def approve_skill(skill_id: str, user_id: str = Depends(get_user_id)):
    from app.skill_store import approve_skill as approve
    try:
        approve(skill_id, user_id=user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "ok"}


@app.post("/skills/{skill_id}/reject")
def reject_skill(skill_id: str, user_id: str = Depends(get_user_id)):
    from app.skill_store import reject_skill as reject
    try:
        reject(skill_id, user_id=user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "ok"}


@app.post("/chat")
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


@app.post("/chat/confirm")
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