"""Cached HTTP requests retain current authorization, deterministic finance and fresh audit."""

from dataclasses import replace
from socket import AF_INET, SOCK_STREAM, socket
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from creditlens.api import create_app
from creditlens.domain import Packet, QueryRequest
from creditlens.errors import ServiceError
from creditlens.local_search import LocalSearchProvider
from creditlens.retrieval import EvidenceCatalog
from creditlens.retrieval_cache import RetrievalCache
from creditlens.settings import Settings
from creditlens.storage import audit_events, grants
from creditlens.workflow import QueryWorkflow
from tests.test_auth import production_settings
from tests.test_retrieval_cache import MemoryBytes

BODY = {
    "borrower_id": "borrower-001",
    "question": "Prepare the DSCR underwriting packet",
    "effective_at": "2026-06-01",
}


def substantive(packet: Packet) -> dict:
    """Exclude only runtime/provider metadata when comparing the complete financial artifact."""
    return packet.model_dump(
        exclude={"request_id", "latency_ms", "stages", "cache_hit", "provider_mode"}
    )


def test_cached_http_preserves_packet_and_audits(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hit skips ranking while recomputing finance and recording a new protected packet."""
    with TestClient(create_app(Settings(database_url="sqlite:///:memory:"))) as client:
        plain = Packet.model_validate(client.post("/api/v1/query", json=BODY).json())
        old = client.app.state.workflow
        provider = LocalSearchProvider(old.catalog, old.store)
        observed = Mock(wraps=provider.search)
        monkeypatch.setattr(provider, "search", observed)
        backend = MemoryBytes()
        cached = RetrievalCache(provider, old.store, backend, b"k" * 32, "local-control-v1")
        client.app.state.workflow = QueryWorkflow(old.catalog, old.store, cached)
        first = Packet.model_validate(client.post("/api/v1/query", json=BODY).json())
        hit = Packet.model_validate(client.post("/api/v1/query", json=BODY).json())
        assert substantive(plain) == substantive(first) == substantive(hit)
        assert plain.provider_mode == first.provider_mode == hit.provider_mode == "local-extractive"
        assert not first.cache_hit and hit.cache_hit and observed.call_count == 1
        assert "cache.retrieval.hit" in [stage.name for stage in hit.stages]
        assert plain.request_id != first.request_id != hit.request_id
        with old.store.engine.connect() as connection:
            rows = connection.execute(select(audit_events)).mappings().all()
        assert len(rows) == 3
        assert rows[-1]["event"]["protected_packet"]["cache_hit"] is True
        assert rows[-1]["event"]["search_provider_mode"] == "local-bm25"
        assert rows[-1]["event"]["protected_packet"]["calculated_metrics"]
        backend.fail_read = backend.fail_write = True
        fallback = Packet.model_validate(client.post("/api/v1/query", json=BODY).json())
        assert substantive(fallback) == substantive(plain) and not fallback.cache_hit
        assert "cache.retrieval.unavailable" in [stage.name for stage in fallback.stages]
        old.catalog.revoke(hit.evidence[0].chunk_id)
        assert (
            client.get(
                f"/api/v1/evidence/{hit.evidence[0].chunk_id}",
                params={"borrower_id": BODY["borrower_id"], "effective_at": BODY["effective_at"]},
            ).status_code
            == 404
        )
        reads = backend.reads
        with old.store.engine.begin() as connection:
            connection.execute(grants.update().values(enabled=False, revision=2))
        assert client.post("/api/v1/query", json=BODY).status_code == 403
        assert backend.reads == reads


@pytest.mark.parametrize("fault", ["principal", "request", "revision", "duplicate", "altered"])
def test_workflow_rejects_bad_provider_results(fault: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a provider with a broken verifier cannot return altered or mismatched evidence."""
    with TestClient(create_app(Settings(database_url="sqlite:///:memory:"))) as client:
        old = client.app.state.workflow
        provider = LocalSearchProvider(old.catalog, old.store)
        principal = old.store.resolve("synthetic-demo")
        query = QueryRequest.model_validate(BODY)
        good = provider.search(query, principal)
        replacements = {
            "principal": replace(good, principal=principal.model_copy(update={"role": "admin"})),
            "request": replace(good, request=query.model_copy(update={"question": "different"})),
            "revision": replace(good, catalog_revision=good.catalog_revision + 1),
            "duplicate": replace(good, chunks=(good.chunks[0],) * 2),
            "altered": replace(
                good, chunks=(good.chunks[0].model_copy(update={"text": "invented evidence"}),)
            ),
        }
        monkeypatch.setattr(provider, "search", Mock(return_value=replacements[fault]))
        monkeypatch.setattr(provider, "verify", Mock())
        workflow = QueryWorkflow(old.catalog, old.store, provider)
        with pytest.raises(ServiceError, match="invalid_search_result"):
            workflow.query(query, principal)
        with old.store.engine.connect() as connection:
            assert not connection.execute(select(audit_events)).all()


def test_provider_catalog_identity_and_final_recheck(monkeypatch: pytest.MonkeyPatch) -> None:
    """Independent catalogs and a provider swap during the request invalidate the workflow."""
    with TestClient(create_app(Settings(database_url="sqlite:///:memory:"))) as client:
        old = client.app.state.workflow
        provider = LocalSearchProvider(EvidenceCatalog(()), old.store)
        with pytest.raises(ValueError, match="share"):
            QueryWorkflow(old.catalog, old.store, provider)
        provider.catalog = old.catalog
        workflow = QueryWorkflow(old.catalog, old.store, provider)
        provider.catalog = EvidenceCatalog(())
        with pytest.raises(ServiceError, match="access_changed"):
            workflow.query(QueryRequest.model_validate(BODY), old.store.resolve("synthetic-demo"))
        provider.catalog = old.catalog
        verify = Mock(side_effect=[None, None, None, ServiceError("revoked", "Revoked", 409)])
        monkeypatch.setattr(provider, "verify", verify)
        with pytest.raises(ServiceError, match="revoked"):
            workflow.query(QueryRequest.model_validate(BODY), old.store.resolve("synthetic-demo"))


@pytest.mark.parametrize(
    "values",
    [
        {"redis_url": "redis://localhost:6379"},
        {"cache_signing_key": "k" * 32},
        {"redis_url": "redis://localhost:6379", "cache_signing_key": "short"},
        {"cache_ttl_seconds": 0},
        {"cache_ttl_seconds": 3601},
    ],
)
def test_cache_configuration_rejects_incomplete_or_unbounded_values(values: dict) -> None:
    """A typo must fail startup instead of disguising disabled caching or unbounded retention."""
    with pytest.raises(ValidationError):
        Settings(**values)


def test_production_cache_requires_governed_authority() -> None:
    """Configured production Redis must have a governed workflow that actually consumes it."""
    values = production_settings().model_dump()
    values.update(
        redis_url=SecretStr("redis://localhost:6379"), cache_signing_key=SecretStr("k" * 32)
    )
    with pytest.raises(ValidationError, match="governed"):
        Settings(**values)


def test_configured_redis_outage_is_visible_without_blocking_packet() -> None:
    """A reserved non-listening loopback port produces a real refused Redis connection."""
    with socket(AF_INET, SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        config = Settings(
            database_url="sqlite:///:memory:",
            redis_url=SecretStr(f"redis://127.0.0.1:{reserved.getsockname()[1]}"),
            cache_signing_key=SecretStr("test-runtime-key-" * 3),
        )
        with TestClient(create_app(config)) as client:
            response = client.post("/api/v1/query", json=BODY)
            assert response.status_code == 200
            result = response.json()
            assert result["policy_disposition"] == "MEETS_POLICY" and not result["cache_hit"]
            assert "cache.retrieval.unavailable" in [s["name"] for s in result["stages"]]
            assert "test-runtime-key" not in response.text


def test_catalog_swap_during_finance_invalidates_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """An equal-epoch catalog replacement cannot pass the final provider identity checkpoint."""
    import creditlens.workflow as workflow_module

    with TestClient(create_app(Settings(database_url="sqlite:///:memory:"))) as client:
        old = client.app.state.workflow
        provider = LocalSearchProvider(old.catalog, old.store)
        original = workflow_module.calculate_review

        def replace_catalog(evidence: tuple) -> object:
            """Swap authority after ranking to reproduce a provider reconfiguration race."""
            result = original(evidence)
            provider.catalog = EvidenceCatalog(())
            return result

        monkeypatch.setattr(workflow_module, "calculate_review", replace_catalog)
        client.app.state.workflow = QueryWorkflow(old.catalog, old.store, provider)
        response = client.post("/api/v1/query", json=BODY)
        assert response.status_code == 409
