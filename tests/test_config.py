import pytest
from app.config import validate_config


def test_validate_config_missing_keys(monkeypatch):
    monkeypatch.setattr("app.config.OLLAMA_API_KEY", "")
    monkeypatch.setattr("app.config.DATABASE_URL", "")
    with pytest.raises(RuntimeError, match="Missing required config"):
        validate_config()


def test_validate_config_passes(monkeypatch):
    monkeypatch.setattr("app.config.OLLAMA_API_KEY", "test-key")
    monkeypatch.setattr("app.config.OLLAMA_BASE_URL", "http://localhost:1")
    monkeypatch.setattr("app.config.LLM_MODEL", "test-model")
    monkeypatch.setattr("app.config.DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    validate_config()