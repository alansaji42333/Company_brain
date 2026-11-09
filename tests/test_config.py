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


# --- Auth0 hardening (session 4) -------------------------------------------

def _auth0_ready(monkeypatch, *, audience="https://company-brain.api", cors="https://app.example.com"):
    monkeypatch.setattr("app.config.OLLAMA_API_KEY", "test-key")
    monkeypatch.setattr("app.config.OLLAMA_BASE_URL", "http://localhost:1")
    monkeypatch.setattr("app.config.LLM_MODEL", "test-model")
    monkeypatch.setattr("app.config.DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    monkeypatch.setattr("app.config.AUTH0_DOMAIN", "tenant.auth0.com")
    monkeypatch.setattr("app.config.AUTH0_AUDIENCE", audience)
    monkeypatch.setattr("app.config.CORS_ORIGINS", cors)
    monkeypatch.setattr("app.config.GOOGLE_OAUTH_CLIENT_ID", "oauth-client-id")
    monkeypatch.setattr("app.config.GOOGLE_OAUTH_CLIENT_SECRET", "oauth-client-secret")
    monkeypatch.setattr("app.config.GOOGLE_OAUTH_REDIRECT_URI", "https://app.example.com/api/v1/orgs/org_x/connectors/drive/oauth/callback")
    monkeypatch.setattr("app.config.FRONTEND_URL", "https://app.example.com")


def test_auth0_enabled_requires_audience(monkeypatch):
    _auth0_ready(monkeypatch, audience="")
    monkeypatch.setattr("app.config.AUTH0_ENABLED", True)
    with pytest.raises(RuntimeError, match="AUTH0_AUDIENCE"):
        validate_config()


def test_auth0_enabled_refuses_wildcard_cors(monkeypatch):
    _auth0_ready(monkeypatch, cors="*")
    monkeypatch.setattr("app.config.AUTH0_ENABLED", True)
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        validate_config()
    # Whitespace-padded wildcard is still refused.
    monkeypatch.setattr("app.config.CORS_ORIGINS", " * ")
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        validate_config()


def test_auth0_enabled_requires_google_oauth_and_frontend_url(monkeypatch):
    """With real login, self-serve Drive onboarding is part of the setup."""
    _auth0_ready(monkeypatch)
    monkeypatch.setattr("app.config.AUTH0_ENABLED", True)
    for var in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_URI"):
        with monkeypatch.context() as m:
            m.setattr("app.config." + var, "")
            with pytest.raises(RuntimeError, match=var):
                validate_config()

    monkeypatch.setattr("app.config.FRONTEND_URL", "")
    with pytest.raises(RuntimeError, match="FRONTEND_URL"):
        validate_config()


def test_auth0_enabled_with_audience_and_explicit_cors_passes(monkeypatch):
    _auth0_ready(monkeypatch)
    monkeypatch.setattr("app.config.AUTH0_ENABLED", True)
    validate_config()


def test_auth0_disabled_with_partial_google_oauth_warns_only(monkeypatch, caplog):
    """Dev mode (Auth0 off): missing OAuth config must NOT block startup —
    just a warning that UI onboarding is disabled."""
    import logging

    _auth0_ready(monkeypatch)
    monkeypatch.setattr("app.config.AUTH0_ENABLED", False)
    monkeypatch.setattr("app.config.GOOGLE_OAUTH_CLIENT_ID", "")
    with caplog.at_level(logging.WARNING):
        validate_config()
    assert any("Drive OAuth onboarding disabled" in r.getMessage() for r in caplog.records)


def test_auth0_disabled_keeps_dev_mode_permissive(monkeypatch):
    _auth0_ready(monkeypatch)
    monkeypatch.setattr("app.config.AUTH0_ENABLED", False)
    validate_config()