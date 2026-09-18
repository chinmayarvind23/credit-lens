"""Verify model ownership and composed production HTTP behavior without live inference."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from creditlens.api import create_app
from creditlens.errors import ServiceError
from creditlens.ollama_generation import OllamaGenerator
from creditlens.settings import Settings
from creditlens.weaviate_provider import WeaviateHybridProvider
from creditlens.weaviate_store import vector_key
from tests.test_generation_safety import DIGEST, MODEL, QUESTION
from tests.test_generation_safety import backend as backend
from tests.test_generation_safety import evidence as evidence
from tests.test_weaviate_provider import VECTOR, production_config, reply, vector_store


def test_governed_production_requires_explicit_generator():
    """Existing governed retrieval cannot quietly become extractive after omitting generation."""
    with pytest.raises(ValidationError, match="requires a pinned generation model"):
        production_config(generation_model="", generation_digest="")


@pytest.mark.parametrize("setting", ["generation_model", "generation_digest"])
def test_partial_generator_configuration_fails_at_settings_boundary(setting):
    """Model identity is a tag/digest pair, never a late best-effort dependency resolution."""
    with pytest.raises(ValidationError, match="model and digest together"):
        production_config(**{setting: ""})


def test_model_readiness_failure_closes_startup_client(evidence, backend, monkeypatch):
    """The HTTP context unwinds when model identity fails before the workflow is available."""
    from creditlens import runtime

    backend.digest = "b" * 64
    factory = Mock(return_value=backend.client)
    monkeypatch.setattr(runtime, "GenerationClient", factory)
    settings = Settings(generation_model=MODEL, generation_digest=DIGEST)
    with pytest.raises(ServiceError, match="generation_model_changed"):
        with runtime.open_generation(settings):
            pytest.fail("Unverified model identity must not yield a generator")
    factory.assert_called_once_with(trust_env=False, follow_redirects=False)
    assert backend.client.is_closed and backend.chat_calls() == []


def test_default_demo_keeps_extractable_offline_workflow(evidence, monkeypatch):
    """Unconfigured local/browser controls retain their established model-free behavior."""
    from creditlens import runtime

    never = Mock(side_effect=AssertionError("No generation dependency in the default demo"))
    monkeypatch.setattr(runtime, "GenerationClient", never)
    with runtime.open_workflow(Settings(), evidence.store) as workflow:
        assert workflow.generator is None
        packet = workflow.query(QUESTION, evidence.principal)
        assert packet.provider_mode == "local-extractive" and packet.synthesis is None
        assert packet.calculated_metrics == evidence.packet.calculated_metrics
    never.assert_not_called()


def test_production_api_composes_vectors_and_required_generation(evidence, backend, monkeypatch):
    """Real provider composition cannot hide model failure behind a successful retrieval branch."""
    from creditlens import api, neural_search, runtime, sql_catalog

    models = SimpleNamespace(
        revision="model-v1",
        encode_query=lambda question: VECTOR,
        rerank=lambda question, candidates, limit: candidates[:limit],
        close=Mock(),
    )
    candidates, _ = evidence.catalog.snapshot(
        evidence.principal, QUESTION.borrower_id, QUESTION.effective_at
    )
    keys = [vector_key(chunk, "catalog", "model-v1") for chunk in candidates]
    schema_owner = vector_store(lambda request: reply([]))
    schema = schema_owner.schema()
    schema_owner.client.close()
    search_calls = []

    def search_response(request):
        """Exercise the documented vector schema/GraphQL wire shape through real adapters."""
        search_calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=schema)
        query = json.loads(request.content)["query"]
        return reply([] if "__readiness_probe__" in query else keys)

    search_client = httpx.Client(transport=httpx.MockTransport(search_response))
    monkeypatch.setattr(runtime, "ProviderClient", lambda **kwargs: search_client)
    monkeypatch.setattr(runtime, "GenerationClient", lambda **kwargs: backend.client)
    monkeypatch.setattr(api, "open_database", lambda url: evidence.engine)
    monkeypatch.setattr(sql_catalog, "SqlEvidenceCatalog", lambda *args: evidence.catalog)
    monkeypatch.setattr(neural_search, "LocalNeuralRanker", lambda *args, **kwargs: models)
    config = production_config(generation_model=MODEL, generation_digest=DIGEST)
    with TestClient(create_app(config)) as client:
        workflow = client.app.state.workflow
        assert isinstance(workflow.provider, WeaviateHybridProvider)
        assert isinstance(workflow.generator, OllamaGenerator)
        client.app.dependency_overrides[api.current_principal] = lambda: evidence.principal
        assert client.get("/ready").status_code == 200
        response = client.post("/api/v1/query", json=QUESTION.model_dump(mode="json"))
        assert response.status_code == 200, response.text
        packet = response.json()
        assert packet["provider_mode"] == "ollama-rag"
        assert packet["synthesis"]["model_digest"] == DIGEST
        assert packet["policy_disposition"] == evidence.packet.policy_disposition
        assert len(backend.chat_calls()) == 1
        backend.digest = "b" * 64
        assert client.get("/ready").status_code == 503
        failed = client.post("/api/v1/query", json=QUESTION.model_dump(mode="json"))
        assert failed.status_code == 503
        assert failed.json()["error"]["code"] == "generation_model_changed"
        assert "synthesis" not in failed.json() and len(backend.chat_calls()) == 1
    assert backend.client.is_closed and search_client.is_closed
    assert search_calls
    models.close.assert_called_once()
