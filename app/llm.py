import logging
from openai import OpenAI
from app.config import OLLAMA_BASE_URL, OLLAMA_API_KEY, LLM_MODEL, LLM_TIMEOUT

logger = logging.getLogger(__name__)

_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=OLLAMA_BASE_URL,
            api_key=OLLAMA_API_KEY,
            timeout=LLM_TIMEOUT,
        )
    return _client


def chat_completion(
    messages: list[dict],
    system: str = "",
    tools: list[dict] | None = None,
    max_tokens: int = 2048,
) -> dict:
    client = get_client()

    full_messages = []
    if system:
        full_messages.append({"role": "system", "content": system})
    full_messages.extend(messages)

    kwargs = {
        "model": LLM_MODEL,
        "messages": full_messages,
        "max_tokens": max_tokens,
    }
    if tools:
        kwargs["tools"] = tools

    response = client.chat.completions.create(**kwargs)
    return response


def chat_completion_stream(
    messages: list[dict],
    system: str = "",
    tools: list[dict] | None = None,
    max_tokens: int = 2048,
):
    """Yield raw stream chunks from the OpenAI-compatible streaming API.

    The caller inspects `chunk.choices[0].delta` for `content` (text deltas)
    and `tool_calls` (function-call deltas) to drive the agent event loop.
    Returns a generator of ChatCompletionChunk objects.
    """
    client = get_client()

    full_messages = []
    if system:
        full_messages.append({"role": "system", "content": system})
    full_messages.extend(messages)

    kwargs = {
        "model": LLM_MODEL,
        "messages": full_messages,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if tools:
        kwargs["tools"] = tools

    stream = client.chat.completions.create(**kwargs)
    for chunk in stream:
        if chunk.choices:
            yield chunk