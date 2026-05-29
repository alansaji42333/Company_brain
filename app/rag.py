import logging
from anthropic import Anthropic
from app.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, CLAUDE_MAX_TOKENS, TOP_K_RETRIEVAL
from app.vectorstore import query_both

logger = logging.getLogger(__name__)

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def answer_question(question: str, user_id: str | None = None) -> dict:
    results = query_both(question, top_k=TOP_K_RETRIEVAL, user_id=user_id)
    raw_chunks = results["raw"]
    skill_chunks = results["skills"]

    if not raw_chunks and not skill_chunks:
        return {
            "answer": "I couldn't find any relevant documents to answer your question.",
            "sources": [],
        }

    context_parts = []
    seen_sources = {}

    for r in raw_chunks:
        meta = r["metadata"]
        source_type = meta.get("source_type", "drive")
        label = f"[Source: {meta['doc_name']}]"
        context_parts.append(f"{label}\n{r['text']}")
        key = (meta["doc_name"], meta["source_url"], source_type)
        if key not in seen_sources:
            seen_sources[key] = {
                "name": meta["doc_name"],
                "url": meta.get("source_url", ""),
                "type": source_type,
            }

    for r in skill_chunks:
        meta = r["metadata"]
        title = meta.get("doc_name", "Untitled Playbook")
        label = f"[Playbook: {title}]"
        context_parts.append(f"{label}\n{r['text']}")
        key = (f"playbook_{title}", "", "playbook")
        if key not in seen_sources:
            seen_sources[key] = {
                "name": title,
                "url": "",
                "type": "playbook",
            }

    context = "\n\n".join(context_parts)

    system_prompt = (
        "You are a helpful assistant that answers questions based on the provided context. "
        "Answer concisely using only the information in the context. "
        "If the context contains playbooks (labeled [Playbook: ...]), prefer their guidance "
        "over raw source material when both are relevant, since playbooks represent reviewed, "
        "authoritative process descriptions. "
        "If the context does not contain enough information to answer the question, "
        "say 'I don't have enough information to answer that.' Do not make up information."
    )

    user_prompt = f"Context:\n{context}\n\nQuestion: {question}"

    client = _get_client()
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    answer = response.content[0].text if response.content else ""

    return {
        "answer": answer,
        "sources": list(seen_sources.values()),
    }
