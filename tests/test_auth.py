import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from app.server import app
    return TestClient(app)


def test_chat_without_auth_returns_401(client):
    response = client.post("/chat", json={"message": "hello"})
    assert response.status_code == 401


def test_skills_without_auth_returns_401(client):
    response = client.get("/skills")
    assert response.status_code == 401


def test_ingest_without_auth_returns_401(client):
    response = client.post("/api/ingest/drive")
    assert response.status_code == 401


def test_chat_with_no_bearer_prefix_returns_401(client):
    response = client.post(
        "/chat",
        json={"message": "hello"},
        headers={"Authorization": "test-user"},
    )
    assert response.status_code == 401


def test_chat_with_empty_bearer_returns_401(client):
    response = client.post(
        "/chat",
        json={"message": "hello"},
        headers={"Authorization": "Bearer "},
    )
    assert response.status_code == 401


def test_skills_with_no_auth_returns_401(client):
    response = client.get("/skills")
    assert response.status_code == 401