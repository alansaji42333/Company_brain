"""End-to-end integration tests through the FastAPI app.

Mocks: LLM (OpenAI client), ChromaDB retrieval, Google/Slack tool execution.
Real: FastAPI routing, JWT auth, SQLAlchemy conversation persistence (sqlite),
      the agent loop + confirmation gating.
"""
import json
import pytest
from jose import jwt
from fastapi.testclient import TestClient


def _make_token(secret: str, user_id: str = "user1") -> str:
    return jwt.encode({"sub": user_id}, secret, algorithm="HS256")


@pytest.fixture
def client(monkeypatch, tmp_path):
    # Ensure a fresh app instance per test with the overridden config.
    import app.config as config
    import app.auth as auth
    monkeypatch.setattr(config, "JWT_SECRET", "test-secret")
    monkeypatch.setattr(auth, "JWT_SECRET", "test-secret")
    monkeypatch.setattr(config, "AUTH0_ENABLED", False)
    monkeypatch.setattr(auth, "AUTH0_ENABLED", False)

    # Replace the DB engine + session factory with a sqlite in-memory one
    # so tests don't touch Neon.
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    import app.database as database

    test_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    test_session = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(database, "async_session", test_session)

    from app.server import app
    return TestClient(app)


@pytest.fixture
def auth_headers():
    token = _make_token("test-secret", "user1")
    return {"Authorization": f"Bearer {token}"}


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeResponse:
    def __init__(self, message):
        self.choices = [_FakeChoice(message)]


class _FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, id_, name, arguments):
        self.id = id_
        self.function = _FakeFunction(name, arguments)


def _mock_llm(monkeypatch, responses):
    """Feed a scripted list of LLM responses in order."""
    calls = {"i": 0}

    def fake_chat_completion(messages, system="", tools=None, max_tokens=2048):
        i = calls["i"]
        calls["i"] += 1
        return responses[min(i, len(responses) - 1)]

    monkeypatch.setattr("app.agent.chat_completion", fake_chat_completion)
    monkeypatch.setattr("app.agent._call_llm", fake_chat_completion)


def _mock_retrieval(monkeypatch, sources=None):
    """Mock ChromaDB retrieval to return scripted sources (or none)."""
    def fake_query_both(question, top_k=None, user_id=None):
        return {"raw": [], "skills": []}

    monkeypatch.setattr("app.agent.query_both", fake_query_both)


def _mock_tool_execute(monkeypatch, result=None):
    result = result or {"success": True, "message": "ok"}
    monkeypatch.setattr("app.agent.execute_tool", lambda name, inp: result)
    monkeypatch.setattr("app.tools.execute_tool", lambda name, inp: result)


def _init_db(monkeypatch):
    """Create sqlite tables for the in-memory config."""
    import asyncio
    from app.database import Base
    import app.database as database

    async def _create():
        async with database.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())


# --- tests ---------------------------------------------------------------

def test_chat_plain_answer(client, auth_headers, monkeypatch):
    _init_db(monkeypatch)
    _mock_retrieval(monkeypatch)
    _mock_llm(monkeypatch, [_FakeResponse(_FakeMessage(content="Hello there"))])

    res = client.post("/api/v1/chat", json={"message": "hi"}, headers=auth_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["type"] == "message"
    assert data["answer"] == "Hello there"
    assert data["conversation_id"]


def test_chat_proposes_action_then_confirm(client, auth_headers, monkeypatch):
    _init_db(monkeypatch)
    _mock_retrieval(monkeypatch)
    _mock_tool_execute(monkeypatch, {"success": True, "message": "sent"})

    tool_call = _FakeToolCall("call_1", "send_slack_message", json.dumps({
        "channel_id": "C123",
        "message": "hi",
    }))
    _mock_llm(monkeypatch, [
        _FakeResponse(_FakeMessage(content="I will send a message", tool_calls=[tool_call])),
        _FakeResponse(_FakeMessage(content="Done!")),
    ])

    # First call proposes an action.
    res = client.post("/api/v1/chat", json={"message": "send a message"}, headers=auth_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["type"] == "confirmation_required"
    assert data["tool_name"] == "send_slack_message"
    conv_id = data["conversation_id"]

    # Confirm the action -> agent continues with a final answer.
    res2 = client.post(
        "/api/v1/chat/confirm",
        json={"conversation_id": conv_id, "approved": True},
        headers=auth_headers,
    )
    assert res2.status_code == 200, res2.text
    data2 = res2.json()
    assert data2["type"] == "message"
    assert data2["answer"] == "Done!"


def test_chat_decline_action(client, auth_headers, monkeypatch):
    _init_db(monkeypatch)
    _mock_retrieval(monkeypatch)

    tool_call = _FakeToolCall("call_2", "send_slack_message", json.dumps({
        "channel_id": "C123", "message": "hi",
    }))
    _mock_llm(monkeypatch, [
        _FakeResponse(_FakeMessage(content="proposing", tool_calls=[tool_call])),
        _FakeResponse(_FakeMessage(content="Okay, I won't send it.")),
    ])

    res = client.post("/api/v1/chat", json={"message": "send msg"}, headers=auth_headers)
    conv_id = res.json()["conversation_id"]

    res2 = client.post(
        "/api/v1/chat/confirm",
        json={"conversation_id": conv_id, "approved": False},
        headers=auth_headers,
    )
    assert res2.status_code == 200
    assert res2.json()["type"] == "message"


def test_auth0_mode_rejects_shared_secret_token(monkeypatch, auth_headers):
    """When AUTH0 is enabled, shared-secret tokens must be rejected."""
    import app.config as config
    import app.auth as auth
    from app.server import app

    monkeypatch.setattr(config, "AUTH0_ENABLED", True)
    monkeypatch.setattr(auth, "AUTH0_ENABLED", True)
    monkeypatch.setattr(config, "AUTH0_DOMAIN", "test.auth0.com")
    monkeypatch.setattr(auth, "AUTH0_DOMAIN", "test.auth0.com")
    # JWKS fetch will fail / no matching key -> 401.
    client = TestClient(app)
    res = client.post("/api/v1/chat", json={"message": "hi"}, headers=auth_headers)
    assert res.status_code == 401


def test_conversation_isolation(client, auth_headers, monkeypatch):
    """User A cannot confirm user B's conversation."""
    _init_db(monkeypatch)
    _mock_retrieval(monkeypatch)
    _mock_llm(monkeypatch, [_FakeResponse(_FakeMessage(content="ok"))])

    res = client.post("/api/v1/chat", json={"message": "hi"}, headers=auth_headers)
    conv_id = res.json()["conversation_id"]

    # User B's token.
    other_token = _make_token("test-secret", "user2")
    other_headers = {"Authorization": f"Bearer {other_token}"}
    res2 = client.post(
        "/api/v1/chat/confirm",
        json={"conversation_id": conv_id, "approved": True},
        headers=other_headers,
    )
    assert res2.status_code == 200
    assert "not found" in res2.json().get("error", "").lower()