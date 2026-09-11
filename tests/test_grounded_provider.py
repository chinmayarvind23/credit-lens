"""Grounding must preserve current scope, original requests and delegated citation integrity."""

from dataclasses import replace
from datetime import date
from unittest.mock import Mock

import pytest

from creditlens.citations import cite
from creditlens.corpus import build_demo_pages
from creditlens.domain import QueryRequest
from creditlens.errors import ServiceError
from creditlens.local_search import LocalSearchProvider
from creditlens.query_grounding import GROUNDING_VERSION, GroundedProvider
from creditlens.retrieval import EvidenceCatalog
from creditlens.retrieval_cache import RetrievalCache
from creditlens.storage import GrantStore, grants, open_database
from tests.test_retrieval_cache import MemoryBytes


@pytest.fixture
def rig():
    """Use real SQLite grants and canonical lexical retrieval; no model behavior is simulated."""
    engine = open_database("sqlite:///:memory:")
    store = GrantStore(engine)
    store.seed_demo()
    inner = LocalSearchProvider(EvidenceCatalog(build_demo_pages()), store)
    provider = GroundedProvider(inner, store)
    principal = store.resolve("synthetic-demo")
    request = QueryRequest(
        borrower_id="borrower-001",
        question="Which package inputs are missing?",
        effective_at=date(2026, 9, 11),
    )
    try:
        yield provider, inner, store, principal, request
    finally:
        engine.dispose()


def test_original_request_citations_and_cached_identity(rig):
    """A cache hit retains the original question while the underlying search sees scoped context."""
    provider, inner, store, principal, request = rig
    inner.search = Mock(wraps=inner.search)
    result = provider.search(request, principal)
    assert result.request == request
    assert result.source.request.question == "Borrower: Northstar Fabrication. " + request.question
    assert result.source.request.borrower_id == request.borrower_id
    assert result.source.request.effective_at == request.effective_at
    assert provider.citation(result, cite(result.chunks[0])) == result.chunks[0]
    cache = RetrievalCache(provider, store, MemoryBytes(), b"k" * 32, GROUNDING_VERSION)
    first, second = cache.search(request, principal), cache.search(request, principal)
    assert first.chunks == second.chunks and second.cache_state == "hit"
    assert second.request == request and inner.search.call_count == 2
    legacy = RetrievalCache(provider, store, cache.backend, b"k" * 32, "pre-grounding")
    assert legacy.key(request, principal, 1, 10) != cache.key(request, principal, 1, 10)
    assert cache.citation(second, cite(second.chunks[0])) == second.chunks[0]
    with store.engine.begin() as connection:
        connection.execute(grants.update().values(enabled=False, revision=2))
    with pytest.raises(ServiceError):
        cache.search(request, principal)
    assert inner.search.call_count == 2


def test_denial_and_catalog_replacement_precede_model_work(rig):
    """Neither an unauthorized borrower nor a revoked identity reaches the wrapped provider."""
    provider, inner, store, principal, request = rig
    inner.search = Mock(wraps=inner.search)
    with pytest.raises(ServiceError):
        provider.search(request.model_copy(update={"borrower_id": "borrower-999"}), principal)
    inner.catalog = EvidenceCatalog(build_demo_pages())
    with pytest.raises(ServiceError, match="access_changed"):
        provider.search(request, principal)
    inner.catalog = provider.catalog
    with store.engine.begin() as connection:
        connection.execute(grants.update().values(revision=2))
    with pytest.raises(ServiceError, match="access_changed"):
        provider.search(request, principal)
    inner.search.assert_not_called()


@pytest.mark.parametrize("mutation", ["grant", "catalog"])
def test_revocation_during_search_rejects_result(rig, mutation):
    """Revocation between name resolution and final output cannot leave a usable result."""
    provider, inner, store, principal, request = rig
    search = inner.search

    def revoke(*args):
        """Inject a state change after actual retrieval to exercise the final freshness check."""
        result = search(*args)
        if mutation == "grant":
            with store.engine.begin() as connection:
                connection.execute(grants.update().values(revision=2))
        else:
            inner.catalog.revoke(result.chunks[0].chunk_id)
        return result

    inner.search = revoke
    with pytest.raises(ServiceError):
        provider.search(request, principal)


@pytest.mark.parametrize(
    "mutation", ["request", "principal", "source_revision", "revision", "chunks", "mode"]
)
def test_forged_envelopes_are_rejected(rig, mutation):
    """Delegated source metadata must exactly match the reconstructible transformed request."""
    provider, _, _, principal, request = rig
    result = provider.search(request, principal)
    source = result.source
    forged = {
        "request": replace(result, source=replace(source, request=request)),
        "principal": replace(
            result, source=replace(source, principal=replace_principal(principal))
        ),
        "source_revision": replace(result, source=replace(source, catalog_revision=999)),
        "revision": replace(result, catalog_revision=999),
        "chunks": replace(result, chunks=()),
        "mode": replace(result, provider_mode="invented"),
    }[mutation]
    with pytest.raises(ServiceError, match="invalid_search_result"):
        provider.verify(forged)


def replace_principal(principal):
    """Keep a forged source principal structurally valid while changing its authority epoch."""
    return principal.model_copy(update={"revision": 999})


def test_result_type_limits_policy_queries_and_bad_citations(rig):
    """Policy-only text stays unchanged and a delegated wrong citation cannot be substituted."""
    provider, inner, _, principal, request = rig
    request = request.model_copy(update={"question": "Explain annual credit review deadlines."})
    result = provider.search(request, principal)
    assert result.source.request == request
    with pytest.raises(ServiceError, match="invalid_search_result"):
        provider.verify(result.source)
    for limit in (0, 11):
        with pytest.raises(ValueError):
            provider.search(request, principal, limit)
    inner.citation = Mock(return_value=result.chunks[1])
    with pytest.raises(ServiceError, match="invalid_citation"):
        provider.citation(result, cite(result.chunks[0]))


def test_workflow_audit_hashes_original_request(rig):
    """Retrieval context must not replace the user's request identity in protected audit records."""
    from hashlib import sha256

    from sqlalchemy import select

    from creditlens.storage import audit_events
    from creditlens.workflow import QueryWorkflow

    provider, _, store, principal, request = rig
    packet = QueryWorkflow(provider.catalog, store, provider).query(request, principal)
    with store.engine.begin() as connection:
        event = connection.execute(
            select(audit_events.c.event).where(audit_events.c.request_id == packet.request_id)
        ).scalar_one()
    assert event["query_hash"] == sha256(request.model_dump_json().encode()).hexdigest()
    assert event["search_provider_mode"].startswith(GROUNDING_VERSION + ":")
