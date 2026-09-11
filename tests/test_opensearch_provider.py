"""Contract tests distinguish mocked OpenSearch HTTP from the separately recorded live run."""

import json
from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from creditlens.corpus import build_demo_pages
from creditlens.cortex_search import MAX_RESPONSE_BYTES, index_record
from creditlens.domain import Citation, QueryRequest
from creditlens.errors import ServiceError
from creditlens.opensearch_provider import OpenSearchProvider, decode_hits, lexical_body
from creditlens.retrieval import EvidenceCatalog
from creditlens.storage import GrantStore, grants, open_database

REQUEST = QueryRequest(
    borrower_id="borrower-001", question="policy DSCR", effective_at=date(2026, 9, 1)
)


@pytest.fixture
def state() -> Iterator[Any]:
    """Use durable grant semantics and authored canonical pages around the transport boundary."""
    engine = open_database("sqlite:///:memory:")
    store = GrantStore(engine)
    store.seed_demo()
    catalog = EvidenceCatalog(build_demo_pages())
    principal = store.resolve("synthetic-demo")
    yield store, catalog, principal
    engine.dispose()


def envelope(row: dict[str, Any]) -> dict[str, Any]:
    """Build the documented successful OpenSearch hit wrapper with exact index identity."""
    return {
        "timed_out": False,
        "_shards": {"total": 1, "successful": 1, "failed": 0},
        "hits": {
            "hits": [
                {"_index": "test-index", "_id": row["CHUNK_ID"], "_score": 1.0, "_source": row}
            ]
        },
    }


def adapter(state: Any, handler: Any) -> OpenSearchProvider:
    """Construct an HTTPS transport fixture with a visibly synthetic bearer token."""
    return OpenSearchProvider(
        "https://search.example.invalid",
        "test-index",
        httpx.Client(transport=httpx.MockTransport(handler)),
        state[1],
        state[0],
        token=SecretStr("test-only"),
    )


def test_exact_server_filter_and_citations(state: Any) -> None:
    """The actual POST places all six scope restrictions inside the lexical query filter."""
    _, catalog, principal = state
    candidates, _ = catalog.snapshot(principal, REQUEST.borrower_id, REQUEST.effective_at)
    selected = candidates[0]

    def handler(request: httpx.Request) -> httpx.Response:
        """Inspect the transport boundary rather than merely testing helper serialization."""
        assert request.url.path == "/test-index/_search"
        assert request.url.params["allow_partial_search_results"] == "false"
        assert request.headers["Authorization"] == "Bearer test-only"
        body = json.loads(request.content)
        filters = body["query"]["bool"]["filter"]
        assert filters[0] == {"term": {"TENANT_ID": "demo-bank"}}
        assert filters[1] == {"terms": {"BORROWER_SCOPE": ["borrower-001", ":shared-policy:"]}}
        assert filters[2] == {"terms": {"ACL_GROUPS": ["underwriting"]}}
        assert filters[3] == {"range": {"VALID_FROM": {"lte": "2026-09-01"}}}
        assert filters[4] == {"range": {"VALID_TO_EXCLUSIVE": {"gt": "2026-09-01"}}}
        assert filters[5] == {"terms": {"CHUNK_ID": [c.chunk_id for c in candidates]}}
        assert "SEARCH_TEXT" not in body["_source"]
        return httpx.Response(200, json=envelope(index_record(selected)))

    provider = adapter(state, handler)
    result = provider.search(REQUEST, principal)
    assert result.chunks == (selected,)
    citation = Citation(
        chunk_id=selected.chunk_id,
        document_id=selected.document_id,
        document_version=selected.document_version,
        page=selected.page,
    )
    assert provider.citation(result, citation) == selected
    with pytest.raises(ServiceError, match="invalid_citation"):
        provider.citation(result, citation.model_copy(update={"page": 999}))
    catalog.revoke(selected.chunk_id)
    with pytest.raises(ServiceError, match="evidence_changed"):
        provider.citation(result, citation)


@pytest.mark.parametrize("mutation", ["disabled", "borrower", "revision", "tenant", "acl"])
def test_current_scope_precedes_transport(state: Any, mutation: str) -> None:
    """Any changed effective grant denies ranking before a network operation occurs."""
    store, _, principal = state
    values = {
        "disabled": {"enabled": False},
        "borrower": {"borrower_ids": []},
        "revision": {"revision": 2},
        "tenant": {"tenant_id": "other"},
        "acl": {"acl_groups": []},
    }
    with store.engine.begin() as connection:
        connection.execute(grants.update().values(**values[mutation]))

    def unexpected(request: httpx.Request) -> httpx.Response:
        """Transport execution would violate the pre-ranking authorization contract."""
        pytest.fail("Denied scope reached OpenSearch")

    with pytest.raises(ServiceError):
        adapter(state, unexpected).search(REQUEST, principal)


@pytest.mark.parametrize("mutation", ["grant", "page"])
def test_revocation_during_http(state: Any, mutation: str) -> None:
    """Remote success cannot erase a current authorization or source revision change."""
    store, catalog, principal = state
    candidates, _ = catalog.snapshot(principal, REQUEST.borrower_id, REQUEST.effective_at)

    def handler(request: httpx.Request) -> httpx.Response:
        """Change the authoritative state while the remote request is in flight."""
        if mutation == "page":
            catalog.revoke(candidates[0].chunk_id)
        else:
            with store.engine.begin() as connection:
                connection.execute(grants.update().values(revision=2))
        return httpx.Response(200, json=envelope(index_record(candidates[0])))

    with pytest.raises(ServiceError) as error:
        adapter(state, handler).search(REQUEST, principal)
    assert error.value.status == 409


@pytest.mark.parametrize(
    "mutation",
    [
        "timeout",
        "shard_fail",
        "shard_missing",
        "shard_bool",
        "hits_missing",
        "source_missing",
        "wrong_index",
        "wrong_id",
        "score_bool",
        "score_nan",
        "tenant",
        "duplicate",
        "extra",
    ],
)
def test_partial_or_malformed_hits_fail_closed(state: Any, mutation: str) -> None:
    """Validate transport execution and every hit wrapper before accepting canonical provenance."""
    _, catalog, principal = state
    candidates, _ = catalog.snapshot(principal, REQUEST.borrower_id, REQUEST.effective_at)
    body = envelope(index_record(candidates[0]))
    hit = body["hits"]["hits"][0]
    if mutation == "timeout":
        body["timed_out"] = True
    elif mutation.startswith("shard"):
        body["_shards"] = {"total": 1, "successful": 1, "failed": 1}
        if mutation == "shard_missing":
            body.pop("_shards")
        if mutation == "shard_bool":
            body["_shards"]["failed"] = False
    elif mutation == "hits_missing":
        body["hits"] = {}
    elif mutation == "source_missing":
        hit.pop("_source")
    elif mutation == "duplicate":
        body["hits"]["hits"] *= 2
    elif mutation in ("tenant", "extra"):
        hit["_source"]["TENANT_ID" if mutation == "tenant" else "SEARCH_TEXT"] = "untrusted"
    else:
        key, value = {
            "wrong_index": ("_index", "other"),
            "wrong_id": ("_id", "other"),
            "score_bool": ("_score", True),
            "score_nan": ("_score", float("nan")),
        }[mutation]
        hit[key] = value
    with pytest.raises(ValueError):
        decode_hits(json.dumps(body).encode(), candidates, 10, "test-index")


@pytest.mark.parametrize("failure", ["redirect", "server", "bytes", "json", "socket"])
def test_remote_failure_never_retries(state: Any, failure: str) -> None:
    """One request and curated errors retain the original scope and hide remote details."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Simulate a bounded provider failure without network access."""
        calls.append(request)
        if failure == "socket":
            raise httpx.ReadTimeout("private detail", request=request)
        status = {"redirect": 302, "server": 503}.get(failure, 200)
        content = b"x" * (MAX_RESPONSE_BYTES + 1) if failure == "bytes" else b"private"
        return httpx.Response(
            status, content=content, headers={"location": "https://other.invalid"}
        )

    with pytest.raises(ServiceError) as error:
        adapter(state, handler).search(REQUEST, state[2])
    assert error.value.message == "Search is unavailable"
    assert len(calls) == 1


def test_configuration_and_empty_scope_bounds(state: Any) -> None:
    """Only explicit loopback tests allow HTTP and no empty scope becomes an unfiltered query."""
    store, _, principal = state
    client = httpx.Client()
    empty = EvidenceCatalog(())
    with pytest.raises(ValueError):
        OpenSearchProvider("http://127.0.0.1:19200", "test-index", client, empty, store)
    provider = OpenSearchProvider(
        "http://127.0.0.1:19200", "test-index", client, empty, store, allow_local_http=True
    )
    assert provider.search(REQUEST, principal).chunks == ()
    with pytest.raises(ValueError):
        provider.search(REQUEST, principal, 101)
    with pytest.raises(ValueError):
        OpenSearchProvider("https://host", "*", client, empty, store)
    with pytest.raises(ValueError):
        OpenSearchProvider("https://host", "test-index", client, empty, store, timeout_seconds=0)
    with pytest.raises(ServiceError):
        lexical_body(REQUEST, principal, (), 10)
