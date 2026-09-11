"""HTTP contract tests distinguish liveness, readiness and authorization failures."""

import httpx
from fastapi.testclient import TestClient

from creditlens.api import create_app
from creditlens.settings import Settings
from tests.test_auth import production_settings


def test_demo_health_and_scoped_borrowers() -> None:
    """A local application starts and advertises its true mode and fixed synthetic scope."""
    with TestClient(create_app(Settings(database_url="sqlite:///:memory:"))) as client:
        assert client.get("/health").status_code == 200
        readiness = client.get("/ready")
        assert readiness.status_code == 503
        assert readiness.json()["error"]["code"] == "workflow_not_initialized"
        result = client.get("/api/v1/borrowers").json()
        assert result["mode"] == "demo"
        assert len(result["borrowers"]) == 5
        assert client.get("/openapi.json").json()["info"]["title"] == "CreditLens"


def test_production_missing_token_and_search_failure() -> None:
    """A process can be alive while a required provider fails readiness."""
    with TestClient(create_app(production_settings())) as client:
        client.app.state.http = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(503, text="private-provider-error")
            )
        )
        assert client.get("/health").status_code == 200
        response = client.get("/ready")
        assert response.status_code == 503
        assert "private-provider-error" not in response.text
        assert client.get("/api/v1/borrowers").status_code == 401
        client.app.state.http.close()
