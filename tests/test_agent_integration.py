"""Hermetic end-to-end integration tests for the agent.

Uses respx to mock the Ollama Cloud OpenAI-compatible endpoint (no network),
so the real OpenAI SDK is exercised against a scripted HTTP backend. The
OpenAI SDK uses an internal httpx transport, so we build an httpx.Client with
a MockTransport backed by the respx router and inject it into the SDK client.

Real: FastAPI routing, JWT auth, SQLAlchemy persistence (sqlite), the agent
loop, confirmation gating, and audit-log recording.

Covers:
  - Multi-turn tool execution flow with confirmation gating (HTTP).
  - Audit log creation upon tool proposal, confirmation, and decline.
  - End-to-end HTTP /api/v1/chat request-response cycle.
"""
import json
import pytest
import httpx
import respx
from jose import jwt
from fastapi.testclient import TestClient


OLLAMA_URL = "http://ollama.test/v1"


# --- fixtures ------------------------------------------------------------

def _make_token(secret: str, user_id: str = "user1") -> str:
    return jwt.encode({"sub": user_id}, secret, algorithm="HS256")


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    import app.config as config
    import app.auth as auth
    import app.llm as llm

    monkeypatch.setattr(config, "JWT_SECRET", "test-secret")
    monkeypatch.setattr(auth, "JWT_SECRET", "test-secret")
    monkeypatch.setattr(config, "AUTH0_ENABLED", False)
    monkeypatch.setattr(auth, "AUTH0_ENABLED", False)
    monkeypatch.setattr(config, "OLLAMA_BASE_URL", OLLAMA_URL)
    monkeypatch.setattr(llm, "OLLAMA_BASE_URL", OLLAMA_URL)
    monkeypatch.setattr(llm, "_client", None)

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    import app.database as database
    test_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}")
    test_session = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(database, "async_session", test_session)

    # Patch the LLM client factory to build an OpenAI client whose httpx
    # transport is backed by the active respx router (set per-test via the
    # `respx_router` fixture). When no router is active, build a normal client.
    def fake_get_client():
        from openai import OpenAI
        router = getattr(fake_get_client, "_router", None)
        if router is not None:
            http_client = httpx.Client(transport=httpx.MockTransport(router.handler))
            client = OpenAI(
                base_url=OLLAMA_URL, api_key="test-key",
                http_client=http_client, timeout=30,
            )
        else:
            client = OpenAI(base_url=OLLAMA_URL, api_key="test-key", timeout=30)
        return client

    monkeypatch.setattr(llm, "get_client", fake_get_client)
    monkeypatch.setattr("app.agent.chat_completion", lambda **kw: llm.chat_completion(**kw))
    monkeypatch.setattr("app.agent.chat_completion_stream", lambda **kw: llm.chat_completion_stream(**kw))

    from app.server import app
    return TestClient(app)


@pytest.fixture
def respx_router(monkeypatch):
    """Yield an active respx router and wire it into the LLM client factory."""
    router = respx.mock(base_url=OLLAMA_URL)
    router.start()
    # Expose the router to fake_get_client.
    import app.llm as llm
    llm.get_client._router = router  # type: ignore[attr-defined]
    try:
        yield router
    finally:
        llm.get_client._router = None  # type: ignore[attr-defined]
        router.stop()


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {_make_token('test-secret', 'user1')}"}


def _init_db(monkeypatch):
    import asyncio
    from app.database import Base
    import app.database as database

    async def _create():
        async with database.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())


def _mock_retrieval(monkeypatch):
    monkeypatch.setattr("app.agent.query_both", lambda question, top_k=None, user_id=None: {"raw": [], "skills": []})


def _mock_tool_execute(monkeypatch, result=None):
    result = result or {"success": True, "message": "sent"}
    monkeypatch.setattr("app.agent.execute_tool", lambda name, inp: result)
    monkeypatch.setattr("app.tools.execute_tool", lambda name, inp: result)


# --- OpenAI-compatible response builders ----------------------------------

def _completion_response(content: str, tool_calls=None):
    """Build a JSON body for POST /chat/completions (non-streaming)."""
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
    }


def _tool_call(id_: str, name: str, args: dict):
    return {
        "id": id_,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _stream_response(deltas):
    """Build an SSE text body for a streaming chat completion.

    `deltas` is a list of dicts with optional keys: content, tool_calls.
    Each becomes one `data:` chunk. A final `[DONE]` sentinel is appended.
    """
    lines = []
    for d in deltas:
        chunk = {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "test-model",
            "choices": [{"index": 0, "delta": d, "finish_reason": None}],
        }
        lines.append("data: " + json.dumps(chunk))
    lines.append("data: [DONE]")
    lines.append("")
    return "\n".join(lines)


# =====================================================================
# 1. End-to-end HTTP /api/v1/chat request-response cycle
# =====================================================================

def test_e2e_http_chat_plain_answer(app_client, respx_router, auth_headers, monkeypatch):
    _init_db(monkeypatch)
    _mock_retrieval(monkeypatch)

    respx_router.post("/chat/completions").respond(json=_completion_response("Hello there"))

    res = app_client.post("/api/v1/chat", json={"message": "hi"}, headers=auth_headers)

    assert res.status_code == 200, res.text
    data = res.json()
    assert data["type"] == "message"
    assert data["answer"] == "Hello there"
    assert data["conversation_id"]


# =====================================================================
# 2. Multi-turn tool execution flow with confirmation gating (HTTP)
# =====================================================================

def test_multi_turn_tool_flow_with_confirmation(app_client, respx_router, auth_headers, monkeypatch):
    _init_db(monkeypatch)
    _mock_retrieval(monkeypatch)
    _mock_tool_execute(monkeypatch, {"success": True, "message": "Message sent"})

    tool_calls = [_tool_call("call_1", "send_slack_message", {"channel_id": "C1", "message": "hi"})]
    route = respx_router.post("/chat/completions")
    route.side_effect = [
        respx.MockResponse(json=_completion_response("I will send it", tool_calls=tool_calls)),
        respx.MockResponse(json=_completion_response("Done!")),
    ]

    # Turn 1: agent proposes an action requiring confirmation.
    res = app_client.post("/api/v1/chat", json={"message": "send a msg"}, headers=auth_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["type"] == "confirmation_required"
    assert data["tool_name"] == "send_slack_message"
    assert data["tool_input"] == {"channel_id": "C1", "message": "hi"}
    conv_id = data["conversation_id"]

    # Turn 2: user confirms -> tool executes -> agent gives final answer.
    res2 = app_client.post(
        "/api/v1/chat/confirm",
        json={"conversation_id": conv_id, "approved": True},
        headers=auth_headers,
    )
    assert res2.status_code == 200, res2.text
    data2 = res2.json()
    assert data2["type"] == "message"
    assert data2["answer"] == "Done!"
    assert len(route.calls) == 2


def test_multi_turn_tool_flow_decline(app_client, respx_router, auth_headers, monkeypatch):
    _init_db(monkeypatch)
    _mock_retrieval(monkeypatch)

    tool_calls = [_tool_call("call_2", "send_slack_message", {"channel_id": "C1", "message": "hi"})]
    route = respx_router.post("/chat/completions")
    route.side_effect = [
        respx.MockResponse(json=_completion_response("proposing", tool_calls=tool_calls)),
        respx.MockResponse(json=_completion_response("Okay, I won't send it.")),
    ]

    res = app_client.post("/api/v1/chat", json={"message": "send msg"}, headers=auth_headers)
    conv_id = res.json()["conversation_id"]

    res2 = app_client.post(
        "/api/v1/chat/confirm",
        json={"conversation_id": conv_id, "approved": False},
        headers=auth_headers,
    )
    assert res2.status_code == 200
    assert res2.json()["type"] == "message"
    assert res2.json()["answer"] == "Okay, I won't send it."
    assert len(route.calls) == 2


# =====================================================================
# 3. Audit log creation upon confirmation / rejection
# =====================================================================

def _fetch_audit(app_client, auth_headers):
    res = app_client.get("/api/v1/audit", headers=auth_headers)
    assert res.status_code == 200, res.text
    return res.json()["audit"]


def test_audit_log_on_tool_proposed_and_confirmed(app_client, respx_router, auth_headers, monkeypatch):
    _init_db(monkeypatch)
    _mock_retrieval(monkeypatch)
    _mock_tool_execute(monkeypatch, {"success": True, "message": "sent"})

    tool_calls = [_tool_call("call_a", "send_slack_message", {"channel_id": "C1", "message": "hi"})]
    respx_router.post("/chat/completions").side_effect = [
        respx.MockResponse(json=_completion_response("proposing", tool_calls=tool_calls)),
        respx.MockResponse(json=_completion_response("Done")),
    ]

    res = app_client.post("/api/v1/chat", json={"message": "send"}, headers=auth_headers)
    conv_id = res.json()["conversation_id"]
    app_client.post("/api/v1/chat/confirm", json={"conversation_id": conv_id, "approved": True}, headers=auth_headers)

    audit = _fetch_audit(app_client, auth_headers)
    types = [a["action_type"] for a in audit]
    assert "tool_proposed" in types
    assert "tool_confirmed" in types

    proposed = next(a for a in audit if a["action_type"] == "tool_proposed")
    assert proposed["tool_name"] == "send_slack_message"
    assert proposed["conversation_id"] == conv_id
    assert proposed["payload"]["arguments"] == {"channel_id": "C1", "message": "hi"}

    confirmed = next(a for a in audit if a["action_type"] == "tool_confirmed")
    assert confirmed["tool_name"] == "send_slack_message"
    assert confirmed["status"] == "success"
    assert confirmed["payload"]["result"]["success"] is True


def test_audit_log_on_tool_declined(app_client, respx_router, auth_headers, monkeypatch):
    _init_db(monkeypatch)
    _mock_retrieval(monkeypatch)

    tool_calls = [_tool_call("call_b", "send_slack_message", {"channel_id": "C1", "message": "hi"})]
    respx_router.post("/chat/completions").side_effect = [
        respx.MockResponse(json=_completion_response("proposing", tool_calls=tool_calls)),
        respx.MockResponse(json=_completion_response("Okay")),
    ]

    res = app_client.post("/api/v1/chat", json={"message": "send"}, headers=auth_headers)
    conv_id = res.json()["conversation_id"]
    app_client.post("/api/v1/chat/confirm", json={"conversation_id": conv_id, "approved": False}, headers=auth_headers)

    audit = _fetch_audit(app_client, auth_headers)
    declined = next(a for a in audit if a["action_type"] == "tool_declined")
    assert declined["tool_name"] == "send_slack_message"
    assert declined["status"] == "declined"
    assert declined["payload"]["result"]["error"] == "declined"


def test_audit_log_on_skill_approved_and_rejected(app_client, auth_headers, monkeypatch, tmp_path):
    _init_db(monkeypatch)
    import app.config as config
    import app.skill_store as skill_store
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr(config, "SKILLS_DIR", str(skills_dir))
    monkeypatch.setattr(skill_store, "SKILLS_DIR", str(skills_dir))
    skill_store.create_skill(title="Test Skill", summary="s", steps=["a"], source_chunk_ids=[], user_id="user1")

    monkeypatch.setattr("app.skill_store.add_chunks", lambda chunks, collection=None: None)
    app_client.post("/api/v1/skills/test-skill/approve", headers=auth_headers)

    audit = _fetch_audit(app_client, auth_headers)
    approved = next(a for a in audit if a["action_type"] == "skill_approved")
    assert approved["payload"]["skill_id"] == "test-skill"

    monkeypatch.setattr("app.skill_store.delete_by_ids", lambda ids, collection=None: None)
    app_client.post("/api/v1/skills/test-skill/reject", headers=auth_headers)

    audit = _fetch_audit(app_client, auth_headers)
    rejected = next(a for a in audit if a["action_type"] == "skill_rejected")
    assert rejected["payload"]["skill_id"] == "test-skill"


# =====================================================================
# 4. Audit log is scoped to the user
# =====================================================================

def test_audit_is_user_scoped(app_client, respx_router, auth_headers, monkeypatch):
    _init_db(monkeypatch)
    _mock_retrieval(monkeypatch)

    respx_router.post("/chat/completions").respond(json=_completion_response("hello"))
    app_client.post("/api/v1/chat", json={"message": "hi"}, headers=auth_headers)

    other_headers = {"Authorization": f"Bearer {_make_token('test-secret', 'user2')}"}
    other_audit = app_client.get("/api/v1/audit", headers=other_headers).json()["audit"]
    assert other_audit == []


# =====================================================================
# 5. Streaming WS path also records audit (parity with HTTP)
# =====================================================================
#
# The WS streaming path runs the OpenAI SDK in a worker thread that posts
# back to the event loop; combining that with respx's MockTransport under
# the TestClient can deadlock, so here we mock the streaming generator
# directly (the same approach used in tests/test_ws_chat.py) and assert
# that the audit row for the tool proposal is still written through the
# real WS handler + real audit hook.

def test_ws_tool_proposal_records_audit(app_client, monkeypatch):
    _init_db(monkeypatch)
    _mock_retrieval(monkeypatch)

    class _Delta:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls

    class _Choice:
        def __init__(self, delta):
            self.delta = delta

    class _Chunk:
        def __init__(self, delta):
            self.choices = [_Choice(delta)]

    class _Fn:
        def __init__(self, name=None, arguments=None):
            self.name = name
            self.arguments = arguments

    class _TC:
        def __init__(self, index, id_=None, name=None, arguments=None):
            self.index = index
            self.id = id_
            self.function = _Fn(name, arguments)
            self.type = "function"

    tool_chunks = [
        _Chunk(_Delta(tool_calls=[_TC(0, id_="call_w", name="send_slack_message", arguments="")])),
        _Chunk(_Delta(tool_calls=[_TC(0, arguments=json.dumps({"channel_id": "C1", "message": "hi"}))])),
    ]

    def fake_stream(messages, system="", tools=None, max_tokens=2048):
        return iter(tool_chunks)

    monkeypatch.setattr("app.agent._call_llm_stream", fake_stream)
    monkeypatch.setattr("app.agent.chat_completion_stream", fake_stream)

    token = _make_token("test-secret", "user1")
    with app_client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "auth", "token": token})
        ws.send_json({"action": "chat", "message": "send"})
        while True:
            evt = ws.receive_json()
            if evt["type"] == "tool_proposal":
                break

    audit = app_client.get("/api/v1/audit", headers={"Authorization": f"Bearer {token}"}).json()["audit"]
    proposed = [a for a in audit if a["action_type"] == "tool_proposed"]
    assert len(proposed) == 1
    assert proposed[0]["tool_name"] == "send_slack_message"