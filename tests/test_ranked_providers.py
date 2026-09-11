"""Ranking doubles probe authority and failures; these tests claim no model quality metric."""

from collections.abc import Iterator
from dataclasses import replace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from creditlens.api import create_app
from creditlens.citations import cite
from creditlens.domain import QueryRequest
from creditlens.errors import ServiceError
from creditlens.hybrid_provider import HybridProvider
from creditlens.local_search import LocalSearchProvider
from creditlens.rerank_provider import RerankProvider
from creditlens.retrieval import EvidenceCatalog, lexical_rank
from creditlens.settings import Settings
from creditlens.storage import audit_events, grants
from creditlens.workflow import QueryWorkflow

BODY = {
    "borrower_id": "borrower-001",
    "question": "Calculate debt service coverage and policy exceptions",
    "effective_at": "2026-09-11",
}
REQUEST = QueryRequest.model_validate(BODY)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Use real grants, evidence and audit with isolated SQLite per failure scenario."""
    with TestClient(create_app(Settings(database_url="sqlite:///:memory:"))) as active:
        yield active


def composition(client: TestClient) -> tuple:
    """Compose real provider wrappers around explicitly named lexical ranking doubles."""
    workflow = client.app.state.workflow
    lexical = LocalSearchProvider(workflow.catalog, workflow.store)
    dense = LocalSearchProvider(workflow.catalog, workflow.store, mode="test-dense-double")
    ranker = Mock(side_effect=lexical_rank)
    provider = RerankProvider(HybridProvider(lexical, dense), ranker, mode="test-rerank-double")
    return provider, dense, ranker, workflow.store.resolve("synthetic-demo")


def test_reranked_http_and_citations_retain_canonical_authority(client: TestClient) -> None:
    """The composed request persists its actual search mode and exact source checks."""
    provider, _, ranker, principal = composition(client)
    old = client.app.state.workflow
    client.app.state.workflow = QueryWorkflow(old.catalog, old.store, provider)
    response = client.post("/api/v1/query", json=BODY)
    assert response.status_code == 200
    assert response.json()["calculated_metrics"][0]["value"] == "1.5000"
    assert ranker.call_count == 1
    assert len(ranker.call_args.args[1]) <= 40
    with old.store.engine.connect() as connection:
        event = connection.execute(select(audit_events.c.event)).scalar_one()
    assert event["search_provider_mode"].startswith("reranked:test-rerank-double:hybrid-rrf:")
    result = provider.search(REQUEST, principal, 1)
    assert provider.citation(result, cite(result.chunks[0])) == result.chunks[0]
    omitted = next(chunk for chunk in result.source.chunks if chunk not in result.chunks)
    with pytest.raises(ServiceError, match="invalid_citation"):
        provider.citation(result, cite(omitted))
    with pytest.raises(ServiceError, match="invalid_citation"):
        provider.citation(result, cite(result.chunks[0]).model_copy(update={"page": 999}))
    old.catalog.revoke(result.chunks[0].chunk_id)
    with pytest.raises(ServiceError, match="evidence_changed"):
        provider.citation(result, cite(result.chunks[0]))


def test_authorization_precedes_each_ranker(client: TestClient) -> None:
    """Neither the dense scorer nor the reranker can receive a denied borrower's text."""
    provider, dense, ranker, principal = composition(client)
    dense.ranker = Mock(side_effect=lexical_rank)
    with pytest.raises(ServiceError):
        provider.search(REQUEST.model_copy(update={"borrower_id": "borrower-006"}), principal)
    assert dense.ranker.call_count == ranker.call_count == 0
    provider.search(REQUEST, principal)
    for chunk in dense.ranker.call_args.args[1] + ranker.call_args.args[1]:
        assert chunk.tenant_id == principal.tenant_id
        assert chunk.borrower_id in (None, REQUEST.borrower_id)
        assert set(chunk.acl_groups).issubset(principal.acl_groups)
    changed = principal.model_copy(update={"revision": principal.revision + 1})
    calls = dense.ranker.call_count
    with pytest.raises(ServiceError, match="access_changed"):
        dense.search(REQUEST, changed)
    assert dense.ranker.call_count == calls


@pytest.mark.parametrize("stage", ["dense", "rerank"])
@pytest.mark.parametrize("change", ["grant", "catalog", "failure"])
def test_mid_ranking_failure_never_audits_success(
    client: TestClient, stage: str, change: str
) -> None:
    """Grant/catalog races and required model outages propagate without lexical fallback."""
    provider, dense, _, _ = composition(client)
    old = client.app.state.workflow

    def mutation(question, candidates, limit):
        """Inject a concurrent authority change exactly while model work would be executing."""
        if change == "grant":
            with old.store.engine.begin() as connection:
                connection.execute(grants.update().values(revision=2))
        elif change == "catalog":
            old.catalog.revoke(candidates[0].chunk_id)
        else:
            raise ServiceError("model_unavailable", "Model is unavailable", 503)
        return candidates[:limit]

    target = dense if stage == "dense" else provider
    target.ranker = mutation
    client.app.state.workflow = QueryWorkflow(old.catalog, old.store, provider)
    assert client.post("/api/v1/query", json=BODY).status_code in (409, 503)
    with old.store.engine.connect() as connection:
        assert not connection.execute(select(audit_events)).all()


@pytest.mark.parametrize("stage", ["dense", "rerank"])
@pytest.mark.parametrize("fault", ["foreign", "altered", "duplicate", "oversize", "shape", "type"])
def test_bad_model_rankings_are_rejected(client: TestClient, stage: str, fault: str) -> None:
    """A ranker cannot smuggle foreign content, duplicates or malformed output downstream."""
    provider, dense, _, principal = composition(client)
    foreign = dense.catalog.snapshot(principal, "borrower-002", REQUEST.effective_at)[0][0]

    def bad_ranking(question, candidates, limit):
        """Deliberately violate one contract at the model boundary, without a model network call."""
        if fault == "foreign":
            return (foreign.model_copy(update={"borrower_id": "borrower-999"}),)
        if fault == "altered":
            return (candidates[0].model_copy(update={"text": "invented financial figures"}),)
        if fault == "duplicate":
            return (candidates[0], candidates[0])
        if fault == "oversize":
            return (candidates[0],) * (limit + 1)
        if fault == "shape":
            return list(candidates[:limit])
        return ("invalid chunk",)

    target = dense if stage == "dense" else provider
    target.ranker = bad_ranking
    with pytest.raises(ServiceError, match="invalid_search_result"):
        provider.search(REQUEST, principal)


@pytest.mark.parametrize("field", ["request", "principal", "catalog_revision", "chunks"])
def test_invalid_source_is_rejected_before_reranker(
    client: TestClient, field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a broken provider verifier cannot expose a malformed candidate pool to the model."""
    provider, _, ranker, principal = composition(client)
    source = provider.provider.search(REQUEST, principal, 40)
    values = {
        "request": REQUEST.model_copy(update={"question": "another question"}),
        "principal": principal.model_copy(update={"role": "admin"}),
        "catalog_revision": source.catalog_revision + 1,
        "chunks": (source.chunks[0].model_copy(update={"text": "tampered"}),),
    }
    monkeypatch.setattr(
        provider.provider, "search", Mock(return_value=replace(source, **{field: values[field]}))
    )
    monkeypatch.setattr(provider.provider, "verify", Mock())
    with pytest.raises(ServiceError, match="invalid_search_result"):
        provider.search(REQUEST, principal)
    ranker.assert_not_called()


def test_rerank_configuration_and_result_guards(client: TestClient) -> None:
    """Guard initial bounds, substituted authority, result type and altered downstream epochs."""
    provider, dense, _, principal = composition(client)
    for limit in (0, 11, 101):
        with pytest.raises(ValueError):
            provider.search(REQUEST, principal, limit)
    for limit in (0, 101):
        with pytest.raises(ValueError):
            dense.search(REQUEST, principal, limit)
    for count in (0, 101):
        with pytest.raises(ValueError):
            RerankProvider(dense, lexical_rank, mode="test", candidates=count)
    for mode in ("", "x" * 201):
        with pytest.raises(ValueError):
            RerankProvider(dense, lexical_rank, mode=mode)
        with pytest.raises(ValueError):
            LocalSearchProvider(dense.catalog, dense.store, mode=mode)
    result = provider.search(REQUEST, principal)
    with pytest.raises(ServiceError, match="invalid_search_result"):
        provider.verify(result.source)
    with pytest.raises(ServiceError, match="invalid_search_result"):
        provider.verify(replace(result, catalog_revision=result.catalog_revision + 1))
    provider.provider.catalog = EvidenceCatalog(())
    with pytest.raises(ServiceError, match="search_scope_changed"):
        provider.search(REQUEST, principal)
    with pytest.raises(ServiceError, match="search_scope_changed"):
        provider.verify(result)
