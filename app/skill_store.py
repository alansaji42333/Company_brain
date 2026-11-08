import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import frontmatter

from app.config import SKILLS_DIR
from app.vectorstore import COLLECTION_SKILLS, add_chunks, delete_by_ids, stored_chunk_id

logger = logging.getLogger(__name__)

os.makedirs(SKILLS_DIR, exist_ok=True)


def _slugify(title: str) -> str:
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s-]+", "-", slug)
    return slug[:80].rstrip("-")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _org_dir(org_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", org_id)
    path = os.path.join(SKILLS_DIR, safe)
    os.makedirs(path, exist_ok=True)
    return path


def _list_files(org_id: str) -> list[str]:
    d = _org_dir(org_id)
    return sorted(
        f for f in os.listdir(d)
        if f.endswith(".md") and not f.startswith(".")
    )


def _load_status(path: str, filename: str) -> dict[str, Any] | None:
    try:
        with open(path) as f:
            post = frontmatter.load(f)
    except Exception:
        return None
    return {
        "id": post.get("id", filename.replace(".md", "")),
        "title": post.get("title", filename.replace(".md", "")),
        "status": post.get("status", "draft"),
        "created_at": post.get("created_at", ""),
        "updated_at": post.get("updated_at", ""),
        "_post": post,
    }


def list_skills(org_id: str = "", only_approved: bool = False) -> list[dict[str, Any]]:
    """List skills for an org. Members (non-reviewers) see approved skills only."""
    skills = []
    for filename in _list_files(org_id):
        info = _load_status(os.path.join(_org_dir(org_id), filename), filename)
        if info is None:
            continue
        if only_approved and info["status"] != "approved":
            continue
        skills.append({
            "id": info["id"],
            "title": info["title"],
            "status": info["status"],
            "created_at": info["created_at"],
            "updated_at": info["updated_at"],
        })
    return skills


def get_skill(org_id: str, skill_id: str, only_approved: bool = False) -> dict[str, Any] | None:
    path = os.path.join(_org_dir(org_id), f"{skill_id}.md")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            post = frontmatter.load(f)
    except Exception:
        return None
    if only_approved and post.get("status") != "approved":
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


def _skill_doc_id(org_id: str, skill_id: str) -> str:
    """The doc_id under which an org's approved skill is embedded."""
    return f"skill_{org_id}_{skill_id}"


def _skill_stored_id(org_id: str, skill_id: str) -> str:
    """The stored Chroma id of an org skill's embedded vector (chunk 0).

    Single derivation: add_chunks (via vectorstore._chunk_id → stored_chunk_id)
    computes the id for a chunk with doc_id=_skill_doc_id and chunk_index=0
    exactly this way. Approve embeds and reject/edits delete this SAME id.
    """
    return stored_chunk_id(org_id, _skill_doc_id(org_id, skill_id), 0)


def save_skill(org_id: str, skill_id: str, body_content: str):
    path = os.path.join(_org_dir(org_id), f"{skill_id}.md")
    now = _now_iso()

    needs_reapproval = False
    if os.path.exists(path):
        with open(path) as f:
            post = frontmatter.load(f)
        if post.get("status") == "approved":
            # An edit to an approved skill bypasses review: demote to draft and
            # remove the stale embedded vector so it can no longer be
            # retrieved until it is re-approved.
            needs_reapproval = True
            post["status"] = "draft"
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

    if needs_reapproval:
        delete_by_ids([_skill_stored_id(org_id, skill_id)], collection=COLLECTION_SKILLS)
        logger.info("Demoted edited approved skill to draft: %s", skill_id)


def create_skill(org_id: str, title: str, summary: str, steps: list[str], source_chunk_ids: list[str]) -> str:
    """Create a draft skill doc for an org. Returns the skill_id (existing ones dedupe)."""
    skill_id = _slugify(title)
    path = os.path.join(_org_dir(org_id), f"{skill_id}.md")
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


def approve_skill(org_id: str, skill_id: str):
    path = os.path.join(_org_dir(org_id), f"{skill_id}.md")
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
        "doc_id": _skill_doc_id(org_id, skill_id),
        "doc_name": post["title"],
        "source_url": "",
        "chunk_index": 0,
        "text": full_text,
        "source_type": "playbook",
        "ingested_at": _now_iso(),
        "user_id": "",
        "org_id": org_id,
    }
    add_chunks([chunk], collection=COLLECTION_SKILLS)
    logger.info("Approved and embedded skill doc: %s (stored id %s)", skill_id, _skill_stored_id(org_id, skill_id))


def reject_skill(org_id: str, skill_id: str):
    path = os.path.join(_org_dir(org_id), f"{skill_id}.md")
    if not os.path.exists(path):
        raise ValueError(f"Skill {skill_id} not found")

    with open(path) as f:
        post = frontmatter.load(f)

    post["status"] = "rejected"
    post["updated_at"] = _now_iso()
    with open(path, "w") as f:
        f.write(frontmatter.dumps(post))

    delete_by_ids([_skill_stored_id(org_id, skill_id)], collection=COLLECTION_SKILLS)
    logger.info("Rejected and removed skill doc from retrieval: %s", skill_id)