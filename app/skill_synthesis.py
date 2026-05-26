import os
import json
import logging
from datetime import datetime, timezone
from anthropic import Anthropic
from app.config import (
    ANTHROPIC_API_KEY, CLAUDE_MODEL, CLAUDE_MAX_TOKENS_SYNTHESIS,
    SYNTHESIS_CHUNKS_PER_BATCH, LAST_SYNTHESIS_FILE,
)
from app.vectorstore import _get_collection, COLLECTION_NAME
from app.skill_store import create_skill, list_skills

logger = logging.getLogger(__name__)

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def _read_last_synthesis_at() -> str | None:
    if os.path.exists(LAST_SYNTHESIS_FILE):
        with open(LAST_SYNTHESIS_FILE) as f:
            val = f.read().strip()
            return val if val else None
    return None


def _write_last_synthesis_at(timestamp: str):
    with open(LAST_SYNTHESIS_FILE, "w") as f:
        f.write(timestamp)


def _fetch_new_chunks(last_synthesis_at: str | None) -> list[dict]:
    col = _get_collection(COLLECTION_NAME)
    where = None
    if last_synthesis_at:
        where = {"ingested_at": {"$gt": last_synthesis_at}}

    offset = 0
    limit = 1000
    all_chunks = []
    while True:
        results = col.get(limit=limit, offset=offset, where=where, include=["documents", "metadatas"])
        if not results["ids"]:
            break
        for i, doc_id in enumerate(results["ids"]):
            all_chunks.append({
                "id": doc_id,
                "text": results["documents"][i] if results["documents"] else "",
                "metadata": results["metadatas"][i] if results["metadatas"] else {},
            })
        offset += limit
        if len(results["ids"]) < limit:
            break

    return all_chunks


def _batch_chunks(chunks: list[dict]) -> list[list[dict]]:
    batches = []
    for i in range(0, len(chunks), SYNTHESIS_CHUNKS_PER_BATCH):
        batches.append(chunks[i:i + SYNTHESIS_CHUNKS_PER_BATCH])
    return batches


def _synthesize_batch(batch: list[dict]) -> list[dict]:
    client = _get_client()
    existing_titles = {s["title"].lower() for s in list_skills()}

    excerpts = []
    for i, c in enumerate(batch):
        meta = c["metadata"]
        label = f"[Excerpt {i + 1} - Source: {meta.get('doc_name', 'unknown')}]"
        excerpts.append(f"{label}\n{c['text']}")

    context = "\n\n".join(excerpts)

    system_prompt = (
        "You are an analyst extracting operational procedures from internal company communications and documents. "
        "Given the following excerpts, identify any recurring operational procedures or how-to processes "
        "described in them. For each procedure found, return a JSON object with these fields:\n"
        "- title: a concise name for the procedure\n"
        "- summary: a 1-2 sentence description of what it covers\n"
        "- steps: an array of strings describing the steps involved\n"
        "- source_chunk_ids: an array of the excerpt IDs (e.g. 'Excerpt 1', 'Excerpt 2') that this procedure was drawn from\n\n"
        "If no clear procedure is described, return an empty array []. "
        "Return ONLY valid JSON — a top-level array of objects. Do not wrap in markdown fences."
    )

    user_prompt = f"Here are the excerpts to analyze:\n\n{context}"

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS_SYNTHESIS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    answer = response.content[0].text if response.content else "[]"

    try:
        procedures = json.loads(answer)
    except json.JSONDecodeError:
        logger.warning("Failed to parse Claude response as JSON, attempting recovery: %s", answer[:200])
        import re
        match = re.search(r'\[.*\]', answer, re.DOTALL)
        if match:
            try:
                procedures = json.loads(match.group())
            except json.JSONDecodeError:
                procedures = []
        else:
            procedures = []

    if not isinstance(procedures, list):
        procedures = []

    for p in procedures:
        p["source_chunk_ids"] = [
            batch[int(s.split()[-1]) - 1]["id"]
            for s in p.get("source_chunk_ids", [])
            if s.split()[-1].isdigit() and 0 < int(s.split()[-1]) <= len(batch)
        ]

    return procedures


def synthesize() -> dict:
    last_at = _read_last_synthesis_at()
    chunks = _fetch_new_chunks(last_at)

    if not chunks:
        logger.info("No new chunks since last synthesis (or first run with no data)")
        return {"batches_processed": 0, "new_skills": 0, "skipped_duplicates": 0}

    logger.info("Fetched %d new chunk(s) for synthesis", len(chunks))
    batches = _batch_chunks(chunks)
    logger.info("Split into %d batch(es) of %d chunks each", len(batches), SYNTHESIS_CHUNKS_PER_BATCH)

    existing_titles = {s["title"].lower() for s in list_skills()}
    total_new = 0
    total_skipped = 0

    for batch_idx, batch in enumerate(batches):
        logger.info("Processing batch %d/%d...", batch_idx + 1, len(batches))
        procedures = _synthesize_batch(batch)

        for proc in procedures:
            title = proc.get("title", "")
            if not title:
                continue
            if title.lower() in existing_titles:
                total_skipped += 1
                logger.info("Skipping duplicate skill: %s", title)
                continue
            try:
                create_skill(
                    title=title,
                    summary=proc.get("summary", ""),
                    steps=proc.get("steps", []),
                    source_chunk_ids=proc.get("source_chunk_ids", []),
                )
                existing_titles.add(title.lower())
                total_new += 1
            except Exception as e:
                logger.error("Failed to create skill '%s': %s", title, e)

    now = datetime.now(timezone.utc).isoformat()
    _write_last_synthesis_at(now)

    return {
        "batches_processed": len(batches),
        "new_skills": total_new,
        "skipped_duplicates": total_skipped,
    }
