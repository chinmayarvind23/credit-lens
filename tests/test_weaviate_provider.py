"""Exercise vector storage through canonical authorization, transport and runtime boundaries."""

import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from creditlens.api import create_app
from creditlens.corpus import build_demo_pages
from creditlens.domain import Citation, QueryRequest
from creditlens.errors import ServiceError
from creditlens.retrieval import EvidenceCatalog
from creditlens.settings import Settings
from creditlens.storage import GrantStore, grants, open_database
from creditlens.weaviate_provider import WeaviateProvider
from creditlens.weaviate_store import MAX_SCOPE, WeaviateStore, vector_key

REQUEST = QueryRequest(
    borrower_id="borrower-001", question="calculate DSCR", effective_at=date(2026, 9, 1)
)
VECTOR = [1.0] + [0.0] * 383


@pytest.fixture
def state():
    """Use real SQL grant reads around a deterministic vector transport fixture."""
    engine = open_database("sqlite:///:memory:")
    store = GrantStore(engine)
    store.seed_demo()
    catalog = EvidenceCatalog(build_demo_pages())
    principal = store.resolve("synthetic-demo")
    candidates, _ = catalog.snapshot(principal, REQUEST.borrower_id, REQUEST.effective_at)
    yield SimpleNamespace(
        engine=engine, store=store, catalog=catalog, principal=principal, candidates=candidates
    )
    engine.dispose()


def vector_store(handler):
    """No real credentials or network calls enter ordinary contract tests."""
    return WeaviateStore(
        "https://vectors.example.invalid",
        "Evidence",
        httpx.Client(transport=httpx.MockTransport(handler)),
        namespace="catalog",
        revision="model-v1",
        token=SecretStr("test-only"),
    )


def reply(keys):
    """Return the documented GraphQL result shape."""
    return httpx.Response(
        200, json={"data": {"Get": {"Evidence": [{"evidenceKey": key} for key in keys]}}}
    )


def test_scope_citations_and_no_query_writes(state):
    """Both coverage and ANN are prefiltered; canonical provenance is rechecked on citation."""
    calls = []
    keys = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST" and request.url.path == "/v1/graphql"
        assert request.headers["Authorization"] == "Bearer test-only"
        query = json.loads(request.content)["query"]
        assert all(key in query for key in keys)
        assert "calculate DSCR" not in query and "text:" not in query
        assert "nearVector" in query
        return reply(keys)

    vectors = vector_store(handler)
    keys.extend(vectors.keys(state.candidates))
    models = SimpleNamespace(encode_query=Mock(return_value=VECTOR))
    provider = WeaviateProvider(state.catalog, state.store, vectors, models)
    result = provider.search(REQUEST, state.principal, 2)
    assert result.chunks == state.candidates[:2] and len(calls) == 1
    chunk = result.chunks[0]
    citation = Citation(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        document_version=chunk.document_version,
        page=chunk.page,
    )
    assert provider.citation(result, citation) == chunk
    with pytest.raises(ServiceError, match="invalid_citation"):
        provider.citation(result, citation.model_copy(update={"page": 999}))
    state.catalog.revoke(chunk.chunk_id)
    with pytest.raises(ServiceError, match="evidence_changed"):
        provider.citation(result, citation)


@pytest.mark.parametrize("change", ["grant", "catalog"])
def test_revocation_during_remote_search(state, change):
    """A valid remote result cannot outrun a concurrent grant or canonical revision change."""
    keys = []

    def handler(request):
        if "nearVector" in json.loads(request.content)["query"]:
            if change == "grant":
                with state.engine.begin() as connection:
                    connection.execute(grants.update().values(revision=2))
            else:
                state.catalog.revoke(state.candidates[0].chunk_id)
            return reply(keys)
        return reply(keys)

    vectors = vector_store(handler)
    keys.extend(vectors.keys(state.candidates))
    provider = WeaviateProvider(
        state.catalog, state.store, vectors, SimpleNamespace(encode_query=lambda question: VECTOR)
    )
    with pytest.raises(ServiceError, match="access_changed|evidence_changed"):
        provider.search(REQUEST, state.principal, 1)


def test_denied_scope_never_reaches_models_or_network(state):
    """Revoke grants before calling the provider to prove authorization precedes ranking."""
    transport = Mock(side_effect=AssertionError("network must not run"))
    models = SimpleNamespace(encode_query=Mock(side_effect=AssertionError("model must not run")))
    provider = WeaviateProvider(state.catalog, state.store, vector_store(transport), models)
    with state.engine.begin() as connection:
        connection.execute(grants.update().values(enabled=False))
    with pytest.raises(ServiceError):
        provider.search(REQUEST, state.principal)
    transport.assert_not_called()
    models.encode_query.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {"errors": [{"message": "private backend details"}]},
        {"data": None},
        [],
        {"data": {"Get": {"Evidence": [{"evidenceKey": "foreign"}]}}},
        {"data": {"Get": {"Evidence": [{"evidenceKey": []}]}}},
    ],
)
def test_malformed_or_foreign_results_fail_closed(state, payload):
    """Partial GraphQL and foreign identities never produce an underwriting answer."""
    vectors = vector_store(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ServiceError, match="invalid_search_result") as failure:
        vectors.rank("question", state.candidates, 1, lambda question: VECTOR)
    assert "private" not in str(failure.value)


def test_index_coverage_model_binding_and_empty_scope(state):
    """A missing or old model's index is not equivalent to no relevant evidence."""
    vectors = vector_store(lambda request: reply([]))
    encode = Mock(return_value=VECTOR)
    with pytest.raises(ServiceError, match="search_index_incomplete"):
        vectors.rank("question", state.candidates, 1, encode)
    encode.assert_called_once()
    assert vectors.rank("question", (), 1, encode) == ()
    with pytest.raises(ServiceError, match="search_scope_unavailable"):
        vectors.rank("question", (state.candidates[0],) * (MAX_SCOPE + 1), 1, encode)
    chunk = state.candidates[0]
    assert (
        len(
            {
                vector_key(chunk, "catalog", "v1"),
                vector_key(chunk, "catalog", "v2"),
                vector_key(chunk, "another", "v1"),
                vector_key(chunk.model_copy(update={"text": "changed"}), "catalog", "v1"),
            }
        )
        == 4
    )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="private outage"),
        httpx.Response(302, headers={"location": "https://elsewhere.invalid"}),
        httpx.Response(200, text="not json"),
    ],
)
def test_transport_failures_are_sanitized(state, response):
    """No redirects, retries or lexical fallback conceal a required provider failure."""
    with pytest.raises(ServiceError, match="search_unavailable"):
        vector_store(lambda request: response).rank(
            "question", state.candidates, 1, lambda question: VECTOR
        )


def test_batch_item_failure_and_visibility(state):
    """HTTP success must include every successful immutable write and query visibility."""

    def handler(request):
        if request.url.path == "/v1/batch/objects":
            objects = json.loads(request.content)["objects"]
            return httpx.Response(
                200, json=[{"id": obj["id"], "result": {"status": "SUCCESS"}} for obj in objects]
            )
        return reply([])

    vectors = vector_store(handler)
    with pytest.raises(ServiceError, match="search_index_incomplete"):
        vectors.upsert(state.candidates[:1], [VECTOR])
    bad = vector_store(lambda request: httpx.Response(200, json=[{"result": {"status": "FAILED"}}]))
    with pytest.raises(ServiceError, match="search_index_failed"):
        bad.upsert(state.candidates[:1], [VECTOR])


def production_config(**changes):
    """Use explicit governed settings without any Cortex credentials."""
    values = dict(
        mode="production",
        production_search="weaviate",
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        catalog_backend="postgres",
        governed_catalog_id="catalog",
        issuer="https://cognito-idp.us-east-1.amazonaws.com/test",
        client_id="client",
        weaviate_url="https://vectors.example.invalid",
        weaviate_token=SecretStr("test-only"),
        weaviate_collection="Evidence",
        local_model_directory="unused",
    )
    return Settings(**(values | changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "demo"},
        {"governed_catalog_id": ""},
        {"weaviate_token": ""},
        {"weaviate_url": "http://127.0.0.1:8080"},
        {"local_model_directory": ""},
        {"cortex_url": "https://cortex.invalid"},
        {"weaviate_collection": "Injected{query}"},
    ],
)
def test_production_configuration_fails_closed(changes):
    """Mixed providers, weak transport and missing authority cannot start the API."""
    with pytest.raises(ValidationError):
        production_config(**changes)


def test_production_api_composition_readiness_and_shutdown(state, monkeypatch):
    """Exercise the production factory and a full packet through the configured vector branch."""
    from creditlens import api, neural_search, runtime, sql_catalog

    models = SimpleNamespace(
        revision="model-v1",
        encode_query=lambda question: VECTOR,
        rerank=lambda question, candidates, limit: candidates[:limit],
        close=Mock(),
    )
    keys = [vector_key(c, "catalog", "model-v1") for c in state.candidates]
    ready = True
    vectors = vector_store(lambda request: reply([]))

    def handler(request):
        if not ready:
            return httpx.Response(503, text="private database error")
        if request.method == "GET":
            return httpx.Response(200, json=vectors.schema())
        query = json.loads(request.content)["query"]
        return reply([] if "__readiness_probe__" in query else keys)

    backend = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(runtime, "ProviderClient", lambda **kwargs: backend)
    monkeypatch.setattr(api, "open_database", lambda url: state.engine)
    monkeypatch.setattr(sql_catalog, "SqlEvidenceCatalog", lambda *args: state.catalog)
    monkeypatch.setattr(neural_search, "LocalNeuralRanker", lambda *args, **kwargs: models)
    with TestClient(create_app(production_config())) as client:
        assert client.get("/ready").json()["search"] == "weaviate-hybrid"
        assert client.post("/api/v1/query", json=REQUEST.model_dump(mode="json")).status_code == 401
        # Identity issuance is tested separately; this fixture isolates runtime composition.
        client.app.dependency_overrides[api.current_principal] = lambda: state.principal
        result = client.post("/api/v1/query", json=REQUEST.model_dump(mode="json"))
        assert result.status_code == 200, result.text
        assert result.json()["policy_disposition"] == "MEETS_POLICY"
        ready = False
        assert client.get("/ready").status_code == 503
        assert client.post("/api/v1/query", json=REQUEST.model_dump(mode="json")).status_code == 503
    models.close.assert_called_once()
    assert backend.is_closed
