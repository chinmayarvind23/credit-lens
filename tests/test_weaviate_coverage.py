"""Exercise vector admission and transport failures without weakening canonical contracts."""

import gzip
import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr

from creditlens.cortex_search import MAX_RESPONSE_BYTES
from creditlens.errors import ServiceError
from creditlens.neural_search import LocalNeuralRanker
from creditlens.weaviate_store import WeaviateStore
from tests.test_neural_search import CHUNKS
from tests.test_neural_search import numeric_models as numeric_models
from tests.test_weaviate_provider import VECTOR, reply, vector_store
from tests.test_weaviate_provider import state as state


@pytest.mark.parametrize(
    "changes",
    [
        {"collection": "lowercase"},
        {"namespace": ""},
        {"revision": ""},
        {"timeout_seconds": float("nan")},
        {"endpoint": "http://127.0.0.1:18081", "allow_local_http": True},
    ],
)
def test_invalid_storage_configuration_never_uses_transport(changes):
    """Reject ambiguous identities and plaintext credentials before any network operation."""
    transport = Mock(side_effect=AssertionError("Configuration must fail before I/O"))
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        values = dict(
            endpoint="https://vectors.example.invalid",
            collection="Evidence",
            client=client,
            namespace="catalog",
            revision="model-v1",
            token=SecretStr("test-only"),
        )
        with pytest.raises(ValueError):
            WeaviateStore(**(values | changes))
    transport.assert_not_called()


@pytest.mark.parametrize("encoded", [False, True])
def test_response_limits_fail_before_untrusted_payload_is_used(encoded):
    """Compressed bodies and oversized JSON cannot bypass bounded transport admission."""
    body = b'"' + b"x" * MAX_RESPONSE_BYTES + b'"'
    response = (
        httpx.Response(200, content=gzip.compress(b"{}"), headers={"Content-Encoding": "gzip"})
        if encoded
        else httpx.Response(200, content=body)
    )
    vectors = vector_store(lambda request: response)
    with vectors.client, pytest.raises(ServiceError, match="search_unavailable") as error:
        vectors.check_ready()
    assert error.value.message == "Vector search is unavailable"


@pytest.mark.parametrize("fault", ["duplicate", "too_many", "not_a_list"])
def test_invalid_row_collections_cannot_become_ranked_evidence(state, fault):
    """A familiar key does not authorize duplicate rows or a malformed GraphQL envelope."""
    keys = []

    def handler(request):
        """Keep keys valid while isolating row count, uniqueness and container validation."""
        if fault == "not_a_list":
            return httpx.Response(200, json={"data": {"Get": {"Evidence": {}}}})
        return reply([keys[0]] * (2 if fault == "duplicate" else len(keys) + 1))

    vectors = vector_store(handler)
    keys.extend(vectors.keys(state.candidates))
    with vectors.client, pytest.raises(ServiceError, match="invalid_search_result"):
        vectors.rank("question", state.candidates, 1, lambda question: VECTOR)


@pytest.mark.parametrize("vector", [[], [0.0] * 384, [float("nan")] * 384])
def test_invalid_model_vectors_never_reach_the_database(state, vector):
    """Wrong dimensions, undefined cosine and nonfinite output cannot query the vector index."""
    transport = Mock(side_effect=AssertionError("Invalid model output must stay local"))
    vectors = vector_store(transport)
    with vectors.client, pytest.raises(ServiceError, match="invalid_model_output"):
        vectors.rank("question", state.candidates, 1, lambda question: vector)
    transport.assert_not_called()


def test_explicit_collection_creation_checks_contract_and_never_replaces_existing():
    """Provisioning verifies the created schema and query access; conflicts do not delete data."""
    schema = {}
    calls = []

    def handler(request):
        """Retain server state across two provisioning attempts without a real external service."""
        calls.append((request.method, request.url.path))
        if request.url.path == "/v1/schema":
            if schema:
                return httpx.Response(409, json={"error": "already exists"})
            schema.update(json.loads(request.content))
            return httpx.Response(200, json=schema)
        if request.method == "GET":
            return httpx.Response(200, json=schema)
        return reply([])

    vectors = vector_store(handler)
    with vectors.client:
        vectors.create()
        assert calls == [
            ("POST", "/v1/schema"),
            ("GET", "/v1/schema/Evidence"),
            ("POST", "/v1/graphql"),
        ]
        with pytest.raises(ServiceError, match="search_unavailable"):
            vectors.create()
        schema["description"] = "another-catalog-or-model"
        with pytest.raises(ServiceError, match="search_index_invalid"):
            vectors.check_ready()
    assert all(method not in {"DELETE", "PUT", "PATCH"} for method, _ in calls)


def test_invalid_embedding_batch_does_not_write(state):
    """A vector count mismatch must fail atomically before an operator batch reaches storage."""
    transport = Mock(side_effect=AssertionError("Invalid batch must not write"))
    vectors = vector_store(transport)
    with vectors.client, pytest.raises(ValueError, match="bounded embedding batch"):
        vectors.upsert(state.candidates[:1], [])
    transport.assert_not_called()


def test_external_encoder_entrypoints_preserve_admission_and_owned_vectors(numeric_models):
    """Export JSON-safe copies for storage while keeping model budgets and the cache intact."""
    _, embedding, _, _ = numeric_models
    model = LocalNeuralRanker(Path("unused"))
    try:
        assert model.encode_documents(()) == []
        documents = model.encode_documents(CHUNKS)
        query = model.encode_query("question")
        assert len(documents) == len(CHUNKS) and len(query) == 384
        assert all(len(vector) == 384 for vector in documents)
        assert all(type(value) is float for value in query + documents[0])
        documents[0][0] = -100.0
        assert model.encode_documents(CHUNKS)[0][0] == 1.0
        assert embedding.encode_document.call_count == 1
        with pytest.raises(ServiceError, match="model_input_limit"):
            model.encode_documents((CHUNKS[0],) * 101)
        with pytest.raises(ServiceError, match="model_input_limit"):
            model.encode_query("x" * 2001)
        assert embedding.encode_document.call_count == embedding.encode_query.call_count == 1
    finally:
        model.close()


def test_external_query_encoder_rejects_invalid_output_and_recovers(numeric_models):
    """A malformed exported query vector releases inference ownership for the next request."""
    _, embedding, _, np = numeric_models
    model = LocalNeuralRanker(Path("unused"))
    normal = embedding.encode_query.side_effect
    try:
        embedding.encode_query.side_effect = lambda *args, **kwargs: np.full((1, 384), np.nan)
        with pytest.raises(ServiceError, match="invalid_model_output"):
            model.encode_query("question")
        assert not model._lock.locked()
        embedding.encode_query.side_effect = normal
        assert model.encode_query("question") == [1.0] * 384
    finally:
        model.close()
