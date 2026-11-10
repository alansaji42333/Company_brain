"""SPA return route for the Drive OAuth callback.

The callback 302s the browser to FRONTEND_URL/connectors?drive=... — the
production server must serve the SPA shell at exactly that path (and must
NOT install a catch-all that swallows API 404s). Frontend navigation is
initialized from the route (see ConnectorsPage + App.tsx deep-link init);
the result query is read, shown, then URL-cleaned by ConnectorsPage."""
from fastapi.testclient import TestClient

from app.server import app


def test_connectors_route_serves_spa_shell():
    client = TestClient(app)
    res = client.get("/connectors")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    # SPA shell markers, not a JSON 404
    assert "<div" in res.text and "script" in res.text


def test_unknown_api_routes_still_404():
    """No catch-all: an undefined API path keeps its real 404."""
    client = TestClient(app)
    res = client.get("/api/v1/does-not-exist")
    assert res.status_code == 404
