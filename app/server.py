import os
import logging
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Company Brain")


def get_user_id(x_user_id: str | None = Header(None)) -> str:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="X-User-Id header is required")
    return x_user_id


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str


class ConfirmRequest(BaseModel):
    conversation_id: str
    approved: bool


class UpdateSkillRequest(BaseModel):
    content: str


def _read_static(filename: str) -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", filename)
    with open(path) as f:
        return f.read()


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(_read_static("index.html"))


@app.get("/skills-page", response_class=HTMLResponse)
def skills_page():
    return HTMLResponse(_read_static("skills.html"))


@app.post("/ingest")
def ingest(folder_id: str | None = None, user_id: str = Depends(get_user_id)):
    from app.drive_ingest import ingest_drive_folder
    from app.chunking import chunk_documents
    from app.vectorstore import add_chunks

    documents = ingest_drive_folder(folder_id, user_id=user_id)
    chunks = chunk_documents(documents)
    add_chunks(chunks)

    return {
        "status": "ok",
        "files_processed": len(documents),
        "chunks_stored": len(chunks),
    }


@app.post("/ingest/slack")
def ingest_slack(user_id: str = Depends(get_user_id)):
    from app.slack_ingest import ingest_slack as run_ingest
    from app.vectorstore import add_chunks

    chunks = run_ingest(user_id=user_id)
    add_chunks(chunks)

    return {
        "status": "ok",
        "chunks_stored": len(chunks),
    }


@app.post("/synthesize")
def synthesize(user_id: str = Depends(get_user_id)):
    from app.skill_synthesis import synthesize as run_synthesis

    summary = run_synthesis(user_id=user_id)
    return {"status": "ok", **summary}


@app.get("/skills")
def list_skills():
    from app.skill_store import list_skills as ls
    return {"skills": ls()}


@app.get("/skills/{skill_id}")
def get_skill(skill_id: str):
    from app.skill_store import get_skill as gs
    skill = gs(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@app.put("/skills/{skill_id}")
def update_skill(skill_id: str, req: UpdateSkillRequest):
    from app.skill_store import save_skill
    save_skill(skill_id, req.content)
    return {"status": "ok"}


@app.post("/skills/{skill_id}/approve")
def approve_skill(skill_id: str):
    from app.skill_store import approve_skill as approve
    try:
        approve(skill_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "ok"}


@app.post("/skills/{skill_id}/reject")
def reject_skill(skill_id: str):
    from app.skill_store import reject_skill as reject
    try:
        reject(skill_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "ok"}


@app.post("/chat")
async def chat(req: ChatRequest, db: AsyncSession = Depends(get_db), user_id: str = Depends(get_user_id)):
    from app.agent import send_message
    result = await send_message(req.conversation_id, req.message, db, user_id=user_id)
    return result


@app.post("/chat/confirm")
async def chat_confirm(req: ConfirmRequest, db: AsyncSession = Depends(get_db), user_id: str = Depends(get_user_id)):
    from app.agent import confirm_action
    result = await confirm_action(str(req.conversation_id), req.approved, db, user_id=user_id)
    return result
