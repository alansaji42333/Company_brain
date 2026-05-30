import os
import re
import logging
from datetime import datetime, timezone
from typing import Any
import frontmatter
from app.config import SKILLS_DIR
from app.vectorstore import add_chunks, delete_by_ids
from app.vectorstore import COLLECTION_SKILLS

logger = logging.getLogger(__name__)

os.makedirs(SKILLS_DIR, exist_ok=True)


def _slugify(title: str) -> str:
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s-]+", "-", slug)
    return slug[:80].rstrip("-")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _user_dir(user_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", user_id)
    path = os.path.join(SKILLS_DIR, safe)
    os.makedirs(path, exist_ok=True)
    return path


def _list_files(user_id: str) -> list[str]:
    d = _user_dir(user_id)
    return sorted(
        f for f in os.listdir(d)
        if f.endswith(".md") and not f.startswith(".")
    )


def list_skills(user_id: str = "") -> list[dict[str, Any]]:
    skills = []
    for filename in _list_files(user_id):
        path = os.path.join(_user_dir(user_id), filename)
        try:
            with open(path) as f:
                post = frontmatter.load(f)
        except Exception:
            continue
        skills.append({
            "id": post.get("id", filename.replace(".md", "")),
            "title": post.get("title", filename.replace(".md", "")),
            "status": post.get("status", "draft"),
            "created_at": post.get("created_at", ""),
            "updated_at": post.get("updated_at", ""),
        })
    return skills


def get_skill(skill_id: str, user_id: str = "") -> dict[str, Any] | None:
    path = os.path.join(_user_dir(user_id), f"{skill_id}.md")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            post = frontmatter.load(f)
    except Exception:
        return None
    return {
        "id": skill_id,
        "title": post.get("title", ""),
        "status": post.get("status", "draft"),
        "created_at": post.get("created_at", ""),
        "updated_at": post.get("updated_at", ""),
        "source_chunk_ids": post.get("source_chunk_ids", []),
        "content": post.content,
    }


def save_skill(skill_id: str, body_content: str, user_id: str = ""):
    path = os.path.join(_user_dir(user_id), f"{skill_id}.md")
    now = _now_iso()

    if os.path.exists(path):
        with open(path) as f:
            post = frontmatter.load(f)
        post.content = body_content
        post["updated_at"] = now
    else:
        post = frontmatter.Post(body_content)
        post["id"] = skill_id
        post["title"] = skill_id.replace("-", " ").title()
        post["status"] = "draft"
        post["source_chunk_ids"] = []
        post["created_at"] = now
        post["updated_at"] = now

    with open(path, "w") as f:
        f.write(frontmatter.dumps(post))


def create_skill(title: str, summary: str, steps: list[str], source_chunk_ids: list[str], user_id: str = "") -> str:
    skill_id = _slugify(title)
    path = os.path.join(_user_dir(user_id), f"{skill_id}.md")
    if os.path.exists(path):
        return skill_id

    now = _now_iso()
    body = f"{summary}\n\n"
    for i, step in enumerate(steps, 1):
        body += f"{i}. {step}\n"

    post = frontmatter.Post(body)
    post["id"] = skill_id
    post["title"] = title
    post["status"] = "draft"
    post["source_chunk_ids"] = source_chunk_ids
    post["created_at"] = now
    post["updated_at"] = now

    with open(path, "w") as f:
        f.write(frontmatter.dumps(post))

    logger.info("Created skill doc: %s", skill_id)
    return skill_id


def approve_skill(skill_id: str, user_id: str = ""):
    path = os.path.join(_user_dir(user_id), f"{skill_id}.md")
    if not os.path.exists(path):
        raise ValueError(f"Skill {skill_id} not found")

    with open(path) as f:
        post = frontmatter.load(f)

    post["status"] = "approved"
    post["updated_at"] = _now_iso()
    with open(path, "w") as f:
        f.write(frontmatter.dumps(post))

    full_text = f"# {post['title']}\n\n{post.content}"
    chunk = {
        "doc_id": f"skill_{skill_id}",
        "doc_name": post["title"],
        "source_url": "",
        "chunk_index": 0,
        "text": full_text,
        "source_type": "playbook",
        "ingested_at": _now_iso(),
        "user_id": user_id,
    }
    add_chunks([chunk], collection=COLLECTION_SKILLS)
    logger.info("Approved and embedded skill doc: %s", skill_id)


def reject_skill(skill_id: str, user_id: str = ""):
    path = os.path.join(_user_dir(user_id), f"{skill_id}.md")
    if not os.path.exists(path):
        raise ValueError(f"Skill {skill_id} not found")

    with open(path) as f:
        post = frontmatter.load(f)

    post["status"] = "rejected"
    post["updated_at"] = _now_iso()
    with open(path, "w") as f:
        f.write(frontmatter.dumps(post))

    doc_id = f"{user_id}_skill_{skill_id}_0"
    delete_by_ids([doc_id], collection=COLLECTION_SKILLS)
    logger.info("Rejected and removed skill doc from retrieval: %s", skill_id)