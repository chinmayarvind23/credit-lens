"""Mocked REST contracts verify application scope, never claim live Snowflake behavior."""

import json
from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from creditlens.corpus import build_demo_pages
from creditlens.cortex_search import (
    MAX_RESPONSE_BYTES,
    CortexSearchProvider,
    decode_results,
    index_record,
    search_filter,
    validate_endpoint,
)
from creditlens.domain import Citation, Principal, QueryRequest
from creditlens.errors import ServiceError
from creditlens.retrieval import EvidenceCatalog
from creditlens.storage import GrantStore, grants, open_database

ENDPOINT = "https://example.snowflakecomputing.com/api/v2/databases/DB/schemas/PUBLIC/"
ENDPOINT += "cortex-search-services/EVIDENCE:query"
REQUEST = QueryRequest(
    borrower_id="borrower-001", question="What is the DSCR policy?", effective_at=date(2026, 9, 1)
)


@pytest.fixture
def state() -> Iterator[tuple[GrantStore, EvidenceCatalog, Principal]]:
    """Use real SQL grants and canonical page fixtures around a mocked remote transport."""
    engine = open_database("sqlite:///:memory:")
    store = GrantStore(engine)
    store.seed_demo()
    yield store, EvidenceCatalog(build_demo_pages()), store.resolve("synthetic-demo")
    engine.dispose()


def provider(
    state: tuple[GrantStore, EvidenceCatalog, Principal], handler: Any
) -> CortexSearchProvider:
    """Construct an isolated test transport without actual network or provider credentials."""
    store, catalog, _ = state
    return CortexSearchProvider(
        ENDPOINT,
        SecretStr("test-only"),
        httpx.Client(transport=httpx.MockTransport(handler)),
        catalog,
        store,
    )


def test_server_filter_and_canonical_hydration(state: Any) -> None:
    """All six server-owned dimensions precede ranking; provider text is never trusted."""
    store, catalog, principal = state
    candidates, _ = catalog.snapshot(principal, REQUEST.borrower_id, REQUEST.effective_at)
    selected = candidates[0]
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Inspect the actual outgoing HTTP request and return one canonical indexed row."""
        body = json.loads(request.content)
        captured.append(body)
        assert request.headers["authorization"] == "Bearer test-only"
        assert body["filter"] == search_filter(REQUEST, principal, candidates)
        assert len(body["filter"]["@and"]) == 6
        assert "TEXT" not in body["columns"]
        return httpx.Response(200, json={"results": [index_record(selected)], "request_id": "abc"})

    adapter = provider(state, handler)
    result = adapter.search(REQUEST, principal)
    assert len(captured) == 1
    assert result.chunks == (selected,)
    citation = Citation(
        chunk_id=selected.chunk_id,
        document_id=selected.document_id,
        document_version=selected.document_version,
        page=selected.page,
    )
    assert adapter.citation(result, citation) == selected
    with pytest.raises(ServiceError, match="invalid_citation"):
        adapter.citation(result, citation.model_copy(update={"page": 999}))
    catalog.revoke(selected.chunk_id)
    with pytest.raises(ServiceError, match="evidence_changed"):
        adapter.citation(result, citation)


@pytest.mark.parametrize("change", ["disabled", "borrower", "revision", "tenant", "acl"])
def test_current_grants_deny_before_http(state: Any, change: str) -> None:
    """JWT-era scope cannot authorize even a single outbound ranking request after grant changes."""
    store, _, principal = state
    updates = {
        "disabled": {"enabled": False},
        "borrower": {"borrower_ids": []},
        "revision": {"revision": 2},
        "tenant": {"tenant_id": "other"},
        "acl": {"acl_groups": []},
    }
    with store.engine.begin() as connection:
        connection.execute(grants.update().values(**updates[change]))

    def unexpected(request: httpx.Request) -> httpx.Response:
        """A transport call on a denied principal is itself a test failure."""
        pytest.fail("Unauthorized remote request")

    with pytest.raises(ServiceError):
        provider(state, unexpected).search(REQUEST, principal)


@pytest.mark.parametrize("change", ["grant", "evidence"])
def test_mid_request_revocation_fails_closed(state: Any, change: str) -> None:
    """A successful provider response cannot override revocation observed before return."""
    store, catalog, principal = state
    candidates, _ = catalog.snapshot(principal, REQUEST.borrower_id, REQUEST.effective_at)

    def handler(request: httpx.Request) -> httpx.Response:
        """Apply revocation during the remote request to expose the snapshot race."""
        if change == "grant":
            with store.engine.begin() as connection:
                connection.execute(grants.update().values(revision=2))
        else:
            catalog.revoke(candidates[0].chunk_id)
        return httpx.Response(200, json={"results": [index_record(candidates[0])]})

    with pytest.raises(ServiceError) as error:
        provider(state, handler).search(REQUEST, principal)
    assert error.value.status == 409


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "tenant",
        "version",
        "page_type",
        "missing",
        "extra",
        "digest",
        "duplicate",
        "nonrow",
        "envelope",
        "overlimit",
    ],
)
def test_malformed_metadata_rejects_whole_ranking(state: Any, mutation: str) -> None:
    """Provider-owned strings and malformed provenance cannot reach context or citations."""
    _, catalog, principal = state
    candidates, _ = catalog.snapshot(principal, REQUEST.borrower_id, REQUEST.effective_at)
    row = index_record(candidates[0])
    changes = {
        "unknown": ("CHUNK_ID", "unknown"),
        "tenant": ("TENANT_ID", "other"),
        "version": ("DOCUMENT_VERSION", "v999"),
        "page_type": ("PAGE", True),
        "extra": ("TEXT", "injected text"),
        "digest": ("RECORD_SHA256", "wrong"),
    }
    if mutation in changes:
        key, value = changes[mutation]
        row[key] = value
    if mutation == "missing":
        del row["ACL_GROUPS"]
    rows: list[Any] = [row]
    if mutation in ("duplicate", "overlimit"):
        rows *= 2
    if mutation == "nonrow":
        rows = ["invalid"]
    body: Any = {"results": rows} if mutation != "envelope" else []
    with pytest.raises(ValueError):
        decode_results(json.dumps(body).encode(), candidates, 1 if mutation == "overlimit" else 10)


@pytest.mark.parametrize(
    "failure", ["redirect", "rate", "server", "timeout", "bytes", "json", "gzip"]
)
def test_provider_failures_are_bounded_and_curated(state: Any, failure: str) -> None:
    """One attempt and a generic error prevent broad fallback and provider-secret disclosure."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Simulate provider failures without contacting the endpoint."""
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private provider detail", request=request)
        status = {"redirect": 302, "rate": 429, "server": 500}.get(failure, 200)
        payload = b"x" * (MAX_RESPONSE_BYTES + 1) if failure == "bytes" else b"private detail"
        headers = {"location": "https://attacker.invalid"} if failure == "redirect" else {}
        if failure == "gzip":
            headers["content-encoding"] = "br"
        return httpx.Response(status, content=payload, headers=headers)

    with pytest.raises(ServiceError) as error:
        provider(state, handler).search(REQUEST, state[2])
    assert error.value.message == "Search is unavailable"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://example.snowflakecomputing.com",
        "https://snowflakecomputing.com.evil.invalid",
        ENDPOINT + "?token=bad",
        ENDPOINT + "#bad",
        ENDPOINT.replace("https://", "https://user@"),
        ENDPOINT.replace(".com/", ".com:8443/"),
        ENDPOINT.replace(":query", ":delete"),
    ],
)
def test_endpoint_secret_boundary(url: str) -> None:
    """Configuration cannot direct a bearer secret to an arbitrary host or service path."""
    with pytest.raises(ValueError):
        validate_endpoint(url)


def test_empty_scope_and_limit_bounds(state: Any) -> None:
    """Empty evidence avoids HTTP, while overlarge filter scopes are never silently widened."""
    _, catalog, principal = state
    candidates, _ = catalog.snapshot(principal, REQUEST.borrower_id, REQUEST.effective_at)
    with pytest.raises(ServiceError):
        search_filter(REQUEST, principal, candidates * 1024)
    with pytest.raises(ServiceError):
        search_filter(REQUEST.model_copy(update={"effective_at": date.max}), principal, candidates)
    empty = (state[0], EvidenceCatalog(()), principal)

    def unexpected(request: httpx.Request) -> httpx.Response:
        """An empty allowed set must not be sent as an unfiltered remote request."""
        pytest.fail("Empty scope reached remote service")

    adapter = provider(empty, unexpected)
    assert adapter.search(REQUEST, principal).chunks == ()
    with pytest.raises(ValueError):
        adapter.search(REQUEST, principal, 101)


def matches_filter(node: dict[str, Any], row: dict[str, Any]) -> bool:
    """Independently interpret the documented subset to test actual predicate behavior."""
    operation, value = next(iter(node.items()))
    if operation == "@and":
        return all(matches_filter(child, row) for child in value)
    if operation == "@or":
        return any(matches_filter(child, row) for child in value)
    if operation == "@not":
        return not matches_filter(value, row)
    column, expected = next(iter(value.items()))
    if operation == "@eq":
        return bool(row[column] == expected)
    if operation == "@contains":
        return expected in row[column]
    if operation == "@lte":
        return bool(row[column] <= expected)
    raise AssertionError("Unsupported filter operation")


def test_filter_predicates_reject_every_scope_dimension(state: Any) -> None:
    """Mutating each indexed scope dimension fails even with an otherwise allowed chunk ID."""
    _, catalog, principal = state
    candidates, _ = catalog.snapshot(principal, REQUEST.borrower_id, REQUEST.effective_at)
    predicate = search_filter(REQUEST, principal, candidates)
    row = index_record(candidates[0])
    assert matches_filter(predicate, row)
    mutations = {
        "TENANT_ID": "other-bank",
        "BORROWER_SCOPE": "borrower-002",
        "ACL_GROUPS": ["credit-officer"],
        "VALID_FROM": "2026-09-02",
        "VALID_TO_EXCLUSIVE": "2026-09-01",
        "CHUNK_ID": "stale-index-id",
    }
    for column, value in mutations.items():
        assert not matches_filter(predicate, row | {column: value}), column
    assert matches_filter(predicate, row | {"VALID_FROM": "2026-09-01"})
    assert matches_filter(predicate, row | {"VALID_TO_EXCLUSIVE": "2026-09-02"})


def test_revoked_page_absent_from_outbound_allowlist(state: Any) -> None:
    """A stale index may retain a page, but the fresh filter cannot rank that page."""
    _, catalog, principal = state
    candidates, _ = catalog.snapshot(principal, REQUEST.borrower_id, REQUEST.effective_at)
    revoked = candidates[0]
    catalog.revoke(revoked.chunk_id)

    def handler(request: httpx.Request) -> httpx.Response:
        """Validate exclusion at the actual transport boundary and simulate a stale service hit."""
        predicate = json.loads(request.content)["filter"]
        assert not matches_filter(predicate, index_record(revoked))
        return httpx.Response(200, json={"results": [index_record(revoked)]})

    with pytest.raises(ServiceError, match="search_unavailable"):
        provider(state, handler).search(REQUEST, principal)
