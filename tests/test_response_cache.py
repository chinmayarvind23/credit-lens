"""Exercise real workflow reuse, scope invalidation and fresh audit guarantees."""

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from creditlens.api import create_app
from creditlens.domain import QueryRequest
from creditlens.response_cache import ResponseCache
from creditlens.settings import Settings
from creditlens.storage import audit_events, grants

REQUEST = {
    "borrower_id": "borrower-001",
    "question": "Calculate debt service coverage",
    "effective_at": "2026-06-01",
}


def test_hit_reuses_packet_but_revalidates_and_audits() -> None:
    """A warm result has identical substance, independent IDs, current citations and two audits."""
    app = create_app(Settings(database_url="sqlite:///:memory:", response_cache_enabled=True))
    with TestClient(app) as client:
        first = client.post("/api/v1/query", json=REQUEST).json()
        second = client.post("/api/v1/query", json=REQUEST).json()
        assert not first["cache_hit"] and second["cache_hit"]
        assert first["request_id"] != second["request_id"]
        assert "cache.response.hit" in [s["name"] for s in second["stages"]]
        with app.state.store.engine.connect() as connection:
            rows = connection.execute(select(audit_events)).mappings().all()
        assert len(rows) == 2
        assert rows[1]["event"]["protected_packet"]["request_id"] == second["request_id"]
        assert rows[1]["event"]["packet_hash"] != rows[0]["event"]["packet_hash"]
        for packet in (first, second):
            for name in ("request_id", "latency_ms", "stages", "cache_hit"):
                packet.pop(name)
        assert first == second
        older = client.post("/api/v1/query", json={**REQUEST, "effective_at": "2025-01-01"}).json()
        assert not older["cache_hit"]
        assert (
            client.post("/api/v1/query", json={**REQUEST, "borrower_id": "borrower-002"}).json()[
                "policy_disposition"
            ]
            == "EXCEPTION_REQUIRED"
        )
        with app.state.store.engine.begin() as connection:
            connection.execute(grants.update().values(enabled=False, revision=2))
        assert client.post("/api/v1/query", json=REQUEST).status_code == 403


def test_catalog_revocation_and_mid_hit_changes_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed canonical epoch invalidates cached packets even before TTL expiry."""
    app = create_app(Settings(database_url="sqlite:///:memory:", response_cache_enabled=True))
    with TestClient(app) as client:
        first = client.post("/api/v1/query", json=REQUEST).json()
        chunk = first["calculated_metrics"][0]["citations"][0]["chunk_id"]
        app.state.workflow.catalog.revoke(chunk)
        response = client.post("/api/v1/query", json=REQUEST)
        assert response.status_code == 200
        assert not response.json()["cache_hit"]
        assert all(c["chunk_id"] != chunk for c in response.json()["evidence"])
        cache = app.state.workflow.response_cache
        original = cache.get

        def revoke_after_cache_read(key: str):
            """Reproduce a permission change while an otherwise valid warm value is in flight."""
            result = original(key)
            with app.state.store.engine.begin() as connection:
                connection.execute(grants.update().values(enabled=False, revision=2))
            return result

        monkeypatch.setattr(cache, "get", revoke_after_cache_read)
        assert client.post("/api/v1/query", json=REQUEST).status_code in {403, 409}


def test_failed_audit_never_populates_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reusing a response requires the previous result to have completed its audit transaction."""
    app = create_app(Settings(database_url="sqlite:///:memory:", response_cache_enabled=True))
    with TestClient(app, raise_server_exceptions=False) as client:
        original = app.state.store.record

        def fail(*args: object) -> None:
            """Simulate a failed audit without intercepting calculation or source validation."""
            raise RuntimeError("audit write failed")

        monkeypatch.setattr(app.state.store, "record", fail)
        assert client.post("/api/v1/query", json=REQUEST).status_code == 500
        monkeypatch.setattr(app.state.store, "record", original)
        assert not client.post("/api/v1/query", json=REQUEST).json()["cache_hit"]


def test_bounded_lru_ttl_and_full_grant_keys() -> None:
    """Deterministic time checks expiry, eviction and scope dimensions without sleeping."""
    app = create_app(Settings(database_url="sqlite:///:memory:"))
    with TestClient(app) as client:
        client.post("/api/v1/query", json=REQUEST)
        workflow = app.state.workflow
        request = QueryRequest.model_validate(REQUEST)
        principal = app.state.store.resolve("synthetic-demo")
        packet = workflow.query(request, principal)
        now = [0.0]
        cache = ResponseCache(capacity=1, ttl=5, clock=lambda: now[0])
        key = cache.key(request, principal, 1)
        cache.put(key, packet)
        assert cache.get(key) is packet
        cache.put("next", packet)
        assert cache.get(key) is None
        now[0] = 5
        assert cache.get("next") is None
        assert key != cache.key(request, principal.model_copy(update={"revision": 2}), 1)
        assert key != cache.key(request, principal, 2)
        assert key != cache.key(
            request.model_copy(update={"effective_at": date(2025, 1, 1)}), principal, 1
        )
        cache.put("large", packet.model_copy(update={"recommended_next_actions": ("x" * 210_000,)}))
        assert cache.get("large") is None
        assert "Calculate debt" not in json.dumps(list(cache._entries))
    with pytest.raises(ValueError):
        ResponseCache(capacity=0)
