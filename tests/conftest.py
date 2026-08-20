"""Shared test fixtures.

Sets a clean environment for hermetic tests. Tests that need specific config
overrides apply them via monkeypatch on the imported modules.
"""
import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:1")
    monkeypatch.setenv("OLLAMA_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("CORS_ORIGINS", "*")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/15")
    monkeypatch.setenv("AUTH0_ENABLED", "false")
    monkeypatch.setenv("SCHEDULE_ENABLED", "false")
    yield