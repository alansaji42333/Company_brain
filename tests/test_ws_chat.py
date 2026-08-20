"""WebSocket integration tests for /ws/chat real-time streaming.

Mocks the LLM streaming API; verifies the WS protocol: payload auth, token
streaming, tool_proposal safety gate, and tool_confirm continuation.
"""
import json
import pytest
from jose import jwt
from fastapi.testclient import TestClient


def _make_token(secret: str, user_id: str = "user1") -> str:
    return jwt.encode({"sub": user_id}, secret, algorithm="HS256")


# --- fake OpenAI streaming chunk objects ---------------------------------

class _FakeDelta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, delta):
        self.delta = delta


class _FakeChunk:
    def __init__(self, delta):
        self.choices = [_FakeChoice(delta)]


class _FakeFnDelta:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _FakeToolCallDelta:
    def __init__(self, index, id_=None, name=None, arguments=None):
        self.index = index
        self.id = id_
        self.function = _FakeFnDelta(name, arguments)
        self.type = "function"


def _text_chunks(*parts):
    return [_FakeChunk(_FakeDelta(content=p)) for p in parts]


def _tool_call_chunks(tool_id, tool_name, args_dict):
    args_str = json.dumps(args_dict)
    return [
        _FakeChunk(_FakeDelta(tool_calls=[_FakeToolCallDelta(0, id_=tool_id, name=tool_name, arguments="")])),
        _FakeChunk(_FakeDelta(tool_calls=[_FakeToolCallDelta(0, arguments=args_str)])),
    ]


def _mock_stream(monkeypatch, chunk_lists):
    """Feed a scripted list of chunk-lists (one per LLM call) in order."""
    state = {"i": 0}

    def fake_call_llm_stream(messages, system="", tools=None, max_tokens=2048):
        i = state["i"]
        state["i"] += 1
        return iter(chunk_lists[min(i, len(chunk_lists) - 1)])

    monkeypatch.setattr("app.agent._call_llm_stream", fake_call_llm_stream)
    # The agent imports chat_completion_stream at module load; patch that too.
    monkeypatch.setattr("app.agent.chat_completion_stream", fake_call_llm_stream)


def _mock_retrieval(monkeypatch):
    monkeypatch.setattr("app.agent.query_both", lambda question, top_k=None, user_id=None: {"raw": [], "skills": []})


def _mock_tool_execute(monkeypatch, result=None):
    result = result or {"success": True, "message": "sent"}
    monkeypatch.setattr("app.agent.execute_tool", lambda name, inp: result)
    monkeypatch.setattr("app.tools.execute_tool", lambda name, inp: result)


def _init_db(monkeypatch):
    import asyncio
    from app.database import Base
    import app.database as database

    async def _create():
        async with database.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_create())


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    import app.config as config
    import app.auth as auth
    monkeypatch.setattr(config, "JWT_SECRET", "test-secret")
    monkeypatch.setattr(auth, "JWT_SECRET", "test-secret")
    monkeypatch.setattr(config, "AUTH0_ENABLED", False)
    monkeypatch.setattr(auth, "AUTH0_ENABLED", False)

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    import app.database as database
    test_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    test_session = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(database, "async_session", test_session)

    from app.server import app
    return TestClient(app)


# --- tests ---------------------------------------------------------------

def test_ws_auth_with_payload(app_client, monkeypatch):
    _init_db(monkeypatch)
    _mock_retrieval(monkeypatch)
    _mock_stream(monkeypatch, [_text_chunks("ok")])

    token = _make_token("test-secret", "user1")
    with app_client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "auth", "token": token})
        ws.send_json({"action": "chat", "message": "hi"})
        # Should receive a sources event (no auth error / close).
        evt = ws.receive_json()
        assert evt["type"] == "sources", evt


def test_ws_auth_rejected_closes(app_client):
    with app_client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "auth", "token": "bogus"})
        evt = ws.receive_json()
        assert evt["type"] == "error"
        assert "Invalid" in evt["detail"] or "token" in evt["detail"].lower()


def test_ws_no_auth_message_rejected(app_client):
    with app_client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "chat", "message": "hi"})
        evt = ws.receive_json()
        assert evt["type"] == "error"


def test_ws_streams_tokens_and_message(app_client, monkeypatch):
    _init_db(monkeypatch)
    _mock_retrieval(monkeypatch)
    _mock_stream(monkeypatch, [_text_chunks("Hello", " ", "world")])

    token = _make_token("test-secret", "user1")
    with app_client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "auth", "token": token})
        ws.send_json({"action": "chat", "message": "hi"})

        events = []
        while len(events) < 5:
            events.append(ws.receive_json())
            if events[-1].get("type") == "message":
                break

        types = [e["type"] for e in events]
        assert types[0] == "sources"
        assert "token" in types
        assert types[-1] == "message"
        # Token events carry content fragments.
        tokens = [e["content"] for e in events if e["type"] == "token"]
        assert "".join(tokens) == "Hello world"
        assert events[-1]["answer"] == "Hello world"
        assert events[-1]["conversation_id"]


def test_ws_tool_proposal_safety_gate(app_client, monkeypatch):
    _init_db(monkeypatch)
    _mock_retrieval(monkeypatch)
    _mock_stream(monkeypatch, [_tool_call_chunks("call_1", "send_slack_message", {"channel_id": "C1", "message": "hi"})])

    token = _make_token("test-secret", "user1")
    with app_client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "auth", "token": token})
        ws.send_json({"action": "chat", "message": "send a message"})

        events = []
        while True:
            evt = ws.receive_json()
            events.append(evt)
            if evt["type"] == "tool_proposal":
                break

        types = [e["type"] for e in events]
        assert types[0] == "sources"
        assert types[-1] == "tool_proposal"
        prop = events[-1]
        assert prop["tool_name"] == "send_slack_message"
        assert prop["arguments"] == {"channel_id": "C1", "message": "hi"}
        assert prop["tool_id"]
        assert prop["conversation_id"]


def test_ws_tool_confirm_continues_stream(app_client, monkeypatch):
    _init_db(monkeypatch)
    _mock_retrieval(monkeypatch)
    _mock_tool_execute(monkeypatch, {"success": True, "message": "sent"})

    # First call: tool proposal. Second call: plain answer.
    _mock_stream(monkeypatch, [
        _tool_call_chunks("call_1", "send_slack_message", {"channel_id": "C1", "message": "hi"}),
        _text_chunks("Done!"),
    ])

    token = _make_token("test-secret", "user1")
    with app_client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "auth", "token": token})
        ws.send_json({"action": "chat", "message": "send a message"})

        # Read up to the tool_proposal.
        prop = None
        while True:
            evt = ws.receive_json()
            if evt["type"] == "tool_proposal":
                prop = evt
                break

        # Confirm the proposed tool.
        ws.send_json({
            "action": "tool_confirm",
            "conversation_id": prop["conversation_id"],
            "tool_id": prop["tool_id"],
            "approved": True,
        })

        # Expect tool_result then tokens then final message.
        events = []
        while True:
            evt = ws.receive_json()
            events.append(evt)
            if evt["type"] == "message":
                break

        types = [e["type"] for e in events]
        assert "tool_result" in types
        assert types[-1] == "message"
        assert events[-1]["answer"] == "Done!"


def test_ws_unknown_action_rejected(app_client, monkeypatch):
    _init_db(monkeypatch)
    token = _make_token("test-secret", "user1")
    with app_client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"action": "auth", "token": token})
        ws.send_json({"action": "bogus"})
        evt = ws.receive_json()
        assert evt["type"] == "error"
        assert "Unknown action" in evt["detail"]