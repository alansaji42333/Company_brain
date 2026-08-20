import os
import logging
import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from slowapi.errors import RateLimitExceeded

from app.database import get_db
from app.config import (
    CORS_ORIGINS, RATE_LIMIT, limiter,
    validate_config, SCHEDULE_ENABLED,
)
from app.auth import verify_token, AuthError
from app.api_v1 import router as v1_router, ingest_router

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

app = FastAPI(title="Company Brain", version="2.0.0")
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


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# Mount the versioned API and the ingest (background-job) API
app.include_router(v1_router)
app.include_router(ingest_router)


# --- backwards-compatible root routes (no auth) --------------------------
# These mirror the v1 endpoints so existing clients/UIs keep working.

def _user_id_from_header(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization Bearer token required")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        return verify_token(token)
    except AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


def _read_static(filename: str) -> str:
    with open(os.path.join(_STATIC_DIR, filename)) as f:
        return f.read()


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_config()
    from app.embeddings import _get_ef
    _get_ef()

    # Create the ARQ Redis pool and attach it to app state so endpoints can
    # enqueue background jobs via request.app.state.redis.enqueue_job(...).
    # Failure to connect is non-fatal: the sync ingestion endpoints still
    # work; only the async /ingest/* and /ingest/status routes require Redis.
    try:
        from arq import create_pool
        from app.worker import redis_settings
        app.state.redis = await create_pool(redis_settings())
        logger.info("arq redis pool connected")
    except Exception as e:
        logger.warning("arq redis pool unavailable (async ingest disabled): %s", e)
        app.state.redis = None

    if SCHEDULE_ENABLED:
        from app.scheduler import start_scheduler
        start_scheduler(app)
    slog.info("startup_complete")
    yield

    if SCHEDULE_ENABLED:
        from app.scheduler import stop_scheduler
        stop_scheduler()
    if app.state.redis is not None:
        await app.state.redis.close()
        logger.info("arq redis pool closed")
    slog.info("shutdown_complete")


app.router.lifespan_context = lifespan


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
    return index()


# --- backwards-compatible root API routes (proxy to v1) ------------------
# Kept so existing clients / vanilla JS UIs keep working during the
# migration to the versioned /api/v1 endpoints.

from app.api_v1 import (  # noqa: E402
    ChatRequest, ConfirmRequest, UpdateSkillRequest,
)


@app.post("/synthesize")
@limiter.limit(RATE_LIMIT)
def synthesize_root(request: Request, authorization: str | None = Header(None)):
    from app.skill_synthesis import synthesize as run_synthesis
    user_id = _user_id_from_header(authorization)
    return {"status": "ok", **run_synthesis(user_id=user_id)}


@app.get("/skills")
def list_skills_root(authorization: str | None = Header(None)):
    from app.skill_store import list_skills as ls
    user_id = _user_id_from_header(authorization)
    return {"skills": ls(user_id=user_id)}


@app.get("/skills/{skill_id}")
def get_skill_root(skill_id: str, authorization: str | None = Header(None)):
    from app.skill_store import get_skill as gs
    user_id = _user_id_from_header(authorization)
    skill = gs(skill_id, user_id=user_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@app.put("/skills/{skill_id}")
def update_skill_root(skill_id: str, req: UpdateSkillRequest, authorization: str | None = Header(None)):
    from app.skill_store import save_skill
    user_id = _user_id_from_header(authorization)
    save_skill(skill_id, req.content, user_id=user_id)
    return {"status": "ok"}


@app.post("/skills/{skill_id}/approve")
def approve_skill_root(skill_id: str, authorization: str | None = Header(None)):
    from app.skill_store import approve_skill as approve
    user_id = _user_id_from_header(authorization)
    try:
        approve(skill_id, user_id=user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "ok"}


@app.post("/skills/{skill_id}/reject")
def reject_skill_root(skill_id: str, authorization: str | None = Header(None)):
    from app.skill_store import reject_skill as reject
    user_id = _user_id_from_header(authorization)
    try:
        reject(skill_id, user_id=user_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "ok"}


@app.post("/chat")
@limiter.limit(RATE_LIMIT)
async def chat_root(
    request: Request,
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
):
    from app.agent import send_message
    user_id = _user_id_from_header(authorization)
    return await send_message(req.conversation_id, req.message, db, user_id=user_id)


@app.post("/chat/confirm")
@limiter.limit(RATE_LIMIT)
async def chat_confirm_root(
    request: Request,
    req: ConfirmRequest,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
):
    from app.agent import confirm_action
    user_id = _user_id_from_header(authorization)
    return await confirm_action(str(req.conversation_id), req.approved, db, user_id=user_id)


# WebSocket streaming for chat --------------------------------------------

@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    """Real-time chat over WebSocket.

    Authentication: the client may present its JWT either as a WebSocket
    subprotocol (first entry) or in the first message after connect:
      {"action": "auth", "token": "<jwt>"}
    If neither yields a valid token the connection is closed with code 4401.

    Once authenticated the server listens for:
      {"action": "chat", "message": "...", "conversation_id": "..."}
      {"action": "tool_confirm", "tool_id": "...", "approved": true|false}

    The agent streams events back via ws.send_json():
      {"type": "sources", ...}
      {"type": "token", "content": "..."}
      {"type": "tool_proposal", "tool_name": "...", "arguments": {...}, ...}
      {"type": "tool_result", ...}
      {"type": "message", ...}
      {"type": "error", "detail": "..."}
    """
    # --- authenticate -----------------------------------------------------
    # Subprotocol auth: client passes the JWT as the first offered subprotocol.
    subprotocols = ws.headers.get("sec-websocket-protocol", "").split(",")
    sub_token = subprotocols[0].strip() if subprotocols else ""
    if sub_token:
        try:
            user_id = verify_token(sub_token)
            await ws.accept(subprotocol=sub_token)
        except AuthError as e:
            await ws.accept()
            await ws.send_json({"type": "error", "detail": e.detail})
            await ws.close(code=4401)
            return
    else:
        await ws.accept()

        # First message must authenticate.
        try:
            first = await ws.receive_json()
        except WebSocketDisconnect:
            return
        except Exception:
            await ws.send_json({"type": "error", "detail": "Expected JSON auth message"})
            await ws.close(code=4400)
            return

        token = first.get("token") or first.get("action") == "auth" and first.get("token")
        if not token:
            await ws.send_json({"type": "error", "detail": "Authentication required"})
            await ws.close(code=4401)
            return
        try:
            user_id = verify_token(token)
        except AuthError as e:
            await ws.send_json({"type": "error", "detail": e.detail})
            await ws.close(code=4401)
            return

    # --- message loop -----------------------------------------------------
    try:
        while True:
            payload = await ws.receive_json()
            action = payload.get("action")

            if action == "chat":
                message = payload.get("message", "")
                conversation_id = payload.get("conversation_id")
                if not message:
                    await ws.send_json({"type": "error", "detail": "Missing 'message'"})
                    continue
                await _ws_run_stream(ws, user_id, "chat", conversation_id=conversation_id, message=message)

            elif action == "tool_confirm":
                tool_id = payload.get("tool_id")
                approved = bool(payload.get("approved"))
                conversation_id = payload.get("conversation_id")
                if not tool_id or not conversation_id:
                    await ws.send_json({"type": "error", "detail": "tool_confirm requires tool_id and conversation_id"})
                    continue
                await _ws_run_stream(
                    ws, user_id, "confirm",
                    conversation_id=conversation_id, approved=approved, tool_id=tool_id,
                )

            else:
                await ws.send_json({"type": "error", "detail": f"Unknown action: {action!r}"})
    except WebSocketDisconnect:
        return
    except Exception as e:
        logger.exception("WebSocket error: %s", e)
        try:
            await ws.send_json({"type": "error", "detail": "Internal error"})
        except Exception:
            pass


async def _ws_run_stream(ws: WebSocket, user_id: str, kind: str, **kwargs):
    """Drive a streaming agent generator and forward every event to the WS."""
    from app.agent import send_message_stream, confirm_action_stream
    from app.database import async_session

    async with async_session() as db:
        try:
            if kind == "chat":
                gen = send_message_stream(
                    kwargs.get("conversation_id"), kwargs.get("message", ""), db, user_id=user_id,
                )
            else:
                gen = confirm_action_stream(
                    str(kwargs.get("conversation_id")), bool(kwargs.get("approved")), db, user_id=user_id,
                )
            async for evt in gen:
                await ws.send_json(evt)
        except Exception as e:
            await db.rollback()
            await ws.send_json({"type": "error", "detail": str(e)})


# Serve static assets (if any) -------------------------------------------
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")