import tiktoken
from app.config import CHUNK_SIZE, CHUNK_OVERLAP

encoder = tiktoken.get_encoding("cl100k_base")


def token_count(text: str) -> int:
    return len(encoder.encode(text))


def chunk_document(doc: dict, user_id: str = "") -> list[dict]:
    text = doc["text"]
    tokens = encoder.encode(text)
    chunks = []

    if not tokens:
        return chunks

    extra_fields = {k: v for k, v in doc.items() if k not in ("text", "id", "name")}

    start = 0
    chunk_index = 0
    while start < len(tokens):
        end = min(start + CHUNK_SIZE, len(tokens))
        chunk_tokens = tokens[start:end]
        chunk_text = encoder.decode(chunk_tokens)

        chunks.append({
            "doc_id": doc["id"],
            "doc_name": doc["name"],
            "source_url": doc.get("source_url", ""),
            "chunk_index": chunk_index,
            "text": chunk_text,
            "user_id": user_id,
            **extra_fields,
        })

        chunk_index += 1
        if end == len(tokens):
            break
        start += CHUNK_SIZE - CHUNK_OVERLAP

    return chunks


def chunk_documents(documents: list[dict], user_id: str = "") -> list[dict]:
    all_chunks = []
    for doc in documents:
        all_chunks.extend(chunk_document(doc, user_id=user_id))
    return all_chunks