"""Regression guards for reviewed vector indexing and dependency failure boundaries."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr

from creditlens.errors import ServiceError
from creditlens.settings import Settings
from creditlens.storage import grants
from scripts.index_weaviate import synchronize
from tests.test_weaviate_provider import REQUEST, VECTOR, production_config, reply, vector_store
from tests.test_weaviate_provider import state as state


@pytest.mark.parametrize("change", ["grant", "catalog"])
def test_indexing_rechecks_authority_after_encoding(state, change):
    """Slow inference must not bridge a known revocation into a subsequent remote write."""
    with state.engine.begin() as connection:
        connection.execute(grants.update().values(role="admin"))

    def encode(batch):
        """Inject the authority change inside the previously unchecked inference window."""
        if change == "grant":
            with state.engine.begin() as connection:
                connection.execute(grants.update().values(revision=2))
        else:
            state.catalog.revoke(batch[0].chunk_id)
        return [VECTOR for _ in batch]

    upload = Mock()
    with pytest.raises(ServiceError, match="access_changed|evidence_changed"):
        synchronize(
            state.catalog,
            state.store,
            SimpleNamespace(upsert=upload),
            SimpleNamespace(encode_documents=encode),
            state.principal.subject,
            REQUEST.borrower_id,
            REQUEST.effective_at,
        )
    upload.assert_not_called()


@pytest.mark.parametrize("result", [None, [], "private backend detail"])
def test_malformed_batch_result_has_curated_error(state, result):
    """Correct object IDs cannot make malformed nested status wrappers safe to dereference."""
    calls = []

    def handler(request):
        """Keep the batch envelope valid so the nested result is the sole fault."""
        calls.append(request)
        objects = json.loads(request.content)["objects"]
        return httpx.Response(200, json=[{"id": objects[0]["id"], "result": result}])

    vectors = vector_store(handler)
    with vectors.client, pytest.raises(ServiceError, match="search_index_failed") as failure:
        vectors.upsert(state.candidates[:1], [VECTOR])
    assert failure.value.status == 503
    assert failure.value.message == "Vector indexing failed"
    assert len(calls) == 1


@pytest.mark.parametrize("incompatible", ["disabled", "named"])
def test_readiness_rejects_incompatible_vector_configuration(incompatible):
    """An ordinary collection query cannot substitute for the configured vector contract."""
    schema = {}
    calls = []

    def handler(request):
        """Return a coherent schema whose sole incompatible setting is under test."""
        calls.append(request)
        return httpx.Response(200, json=schema)

    vectors = vector_store(handler)
    schema.update(vectors.schema())
    if incompatible == "disabled":
        schema["vectorIndexConfig"]["skip"] = True
    else:
        schema["vectorConfig"] = {"different-space": {"vectorIndexType": "hnsw"}}
    with vectors.client, pytest.raises(ServiceError, match="search_index_invalid"):
        vectors.check_ready()
    assert len(calls) == 1
    assert calls[0].method == "GET"


def test_readiness_failure_releases_models_and_owned_http_client(state, monkeypatch):
    """Startup failure must unwind resources even though the workflow never yields."""
    from contextlib import nullcontext

    from creditlens import neural_search, runtime, sql_catalog

    # Isolate vector startup failure from the independently tested required generation dependency.
    monkeypatch.setattr(runtime, "open_generation", lambda config: nullcontext(None))

    models = SimpleNamespace(revision="model-v1", close=Mock())
    backend = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503, text="private outage"))
    )
    monkeypatch.setattr(runtime, "ProviderClient", lambda **kwargs: backend)
    monkeypatch.setattr(sql_catalog, "SqlEvidenceCatalog", lambda *args: state.catalog)
    monkeypatch.setattr(neural_search, "LocalNeuralRanker", lambda *args, **kwargs: models)
    with pytest.raises(ServiceError, match="search_unavailable"):
        with runtime.open_governed_workflow(production_config(), state.store):
            pytest.fail("An unavailable required vector backend must prevent startup")
    models.close.assert_called_once_with()
    assert backend.is_closed


def test_former_cortex_configuration_keeps_default_provider(monkeypatch):
    """Existing production configurations need no new provider or local model settings."""
    import os

    for name in tuple(os.environ):
        if name.startswith("CREDITLENS_"):
            monkeypatch.delenv(name)
    config = Settings(
        mode="production",
        issuer="https://cognito-idp.us-east-1.amazonaws.com/test",
        client_id="client",
        cortex_url="https://cortex.example.invalid/search",
        cortex_token=SecretStr("test-only"),
    )
    assert config.production_search == "cortex"
    assert config.local_model_directory == ""
    assert config.weaviate_url == ""
    assert Settings().retrieval_mode == "lexical"


def test_partial_vector_visibility_cannot_satisfy_smaller_requested_limit(state):
    """Enough visible hits for top-k still fails when another authorized vector is missing."""
    keys = []
    queries = []

    def handler(request):
        """Model an asynchronously incomplete vector index that can already return top-one."""
        query = json.loads(request.content)["query"]
        queries.append(query)
        return reply(keys[:-1])

    vectors = vector_store(handler)
    keys.extend(vectors.keys(state.candidates))
    assert len(keys) > 1
    with vectors.client, pytest.raises(ServiceError, match="search_index_incomplete"):
        vectors.rank("question", state.candidates, 1, lambda question: VECTOR)
    assert len(queries) == 1
    assert "nearVector" in queries[0]
    assert f"limit:{len(keys)}," in queries[0]
