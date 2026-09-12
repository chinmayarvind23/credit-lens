"""Real numeric kernels and explicit model doubles test budgets, isolation and failure contracts."""

import importlib
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from creditlens import neural_search
from creditlens.api import create_app
from creditlens.corpus import build_demo_pages
from creditlens.errors import ServiceError
from creditlens.neural_search import LocalNeuralRanker, input_budget
from creditlens.observability import Telemetry
from creditlens.retrieval import chunk_page
from creditlens.settings import Settings
from tests.test_auth import production_settings
from tests.test_retrieval_cache import MemoryBytes

CHUNKS = tuple(chunk_page(page)[0] for page in build_demo_pages()[:3])


@pytest.fixture
def neural_traces() -> Iterator[Path]:
    """Keep lifespan telemetry artifacts outside persistent application storage."""
    with TemporaryDirectory(prefix="creditlens-neural-api-traces-") as directory:
        yield Path(directory) / "traces.jsonl"


@pytest.mark.parametrize("operation", ["embed_documents", "embed_query", "rerank"])
@pytest.mark.parametrize("fault", ["exception", "invalid_output"])
def test_neural_operation_metrics_and_recovery(numeric_models: tuple, operation: str, fault: str):
    """Real telemetry separates injected model failures from successful recovery."""
    _, embedding, reranker, np = numeric_models
    target = {
        "embed_documents": embedding.encode_document,
        "embed_query": embedding.encode_query,
        "rerank": reranker.predict,
    }[operation]
    normal = target.side_effect
    with TemporaryDirectory(prefix="creditlens-neural-metrics-") as directory:
        trace = Path(directory) / "traces.jsonl"
        telemetry = Telemetry(str(trace))
        model = LocalNeuralRanker(Path("unused"), telemetry=telemetry)
        target.side_effect = (
            RuntimeError("PRIVATE-MODEL-DETAILS")
            if fault == "exception"
            else lambda *args, **kwargs: np.array([np.nan])
        )
        call = model.rerank if operation == "rerank" else model.rank
        try:
            with pytest.raises(ServiceError):
                call("PRIVATE-QUESTION", CHUNKS)
            target.side_effect = normal
            assert call("PRIVATE-QUESTION", CHUNKS)
            metrics = telemetry.render().decode()
            stage = "neural." + operation
            assert f'creditlens_stage_errors_total{{stage="{stage}"}} 1.0' in metrics
            assert f'creditlens_stage_duration_seconds_count{{stage="{stage}"}} 2.0' in metrics
        finally:
            model.close()
            telemetry.close()
        records = trace.read_text(encoding="utf-8")
        assert "PRIVATE-QUESTION" not in records + metrics
        assert "PRIVATE-MODEL-DETAILS" not in records + metrics
        assert CHUNKS[0].chunk_id not in records + metrics


def test_cached_vectors_do_not_count_as_embedding_calls(numeric_models: tuple):
    """The second rank call reuses document vectors while still observing its new query encoding."""
    with TemporaryDirectory(prefix="creditlens-neural-cache-metrics-") as directory:
        telemetry = Telemetry(str(Path(directory) / "traces.jsonl"))
        model = LocalNeuralRanker(Path("unused"), telemetry=telemetry)
        try:
            model.rank("one", CHUNKS)
            model.rank("two", CHUNKS)
            metrics = telemetry.render().decode()
            assert (
                'creditlens_stage_duration_seconds_count{stage="neural.embed_documents"} 1.0'
                in metrics
            )
            assert (
                'creditlens_stage_duration_seconds_count{stage="neural.embed_query"} 2.0' in metrics
            )
        finally:
            model.close()
            telemetry.close()


@pytest.fixture
def numeric_models(monkeypatch: pytest.MonkeyPatch) -> tuple:
    """Replace only model loaders and weights; preserve real NumPy and all runtime provider code."""
    np = pytest.importorskip("numpy")
    embedding = Mock()
    embedding.encode_document.side_effect = lambda texts, **kw: np.ones(
        (len(texts), 384), dtype="float32"
    )
    embedding.encode_query.side_effect = lambda texts, **kw: np.ones(
        (len(texts), 384), dtype="float32"
    )
    reranker = Mock()
    reranker.predict.side_effect = lambda pairs, **kw: np.ones(len(pairs), dtype="float32")
    sdk = SimpleNamespace(
        SentenceTransformer=Mock(return_value=embedding), CrossEncoder=Mock(return_value=reranker)
    )
    original = importlib.import_module
    fake_torch = Mock()
    monkeypatch.setattr(
        neural_search,
        "verify_bundle",
        lambda path: (path / "embedding", path / "reranker", "fixture-revision"),
    )

    def load(name, *args, **kwargs):
        """Keep unrelated imports real while documenting the two explicit SDK seams."""
        if name == "sentence_transformers":
            return sdk
        if name == "torch":
            return fake_torch
        return original(name, *args, **kwargs)

    monkeypatch.setattr(neural_search.importlib, "import_module", load)
    return sdk, embedding, reranker, np


def test_bounded_embeddings_do_not_cache_questions(numeric_models: tuple) -> None:
    """Reusing scoped text skips encoding, changed text misses, and each query is encoded anew."""
    sdk, embedding, _, _ = numeric_models
    model = LocalNeuralRanker(Path("unused"), cache_entries=2)
    assert sdk.SentenceTransformer.call_args.kwargs["local_files_only"] is True
    assert sdk.CrossEncoder.call_args.kwargs["trust_remote_code"] is False
    assert sdk.CrossEncoder.call_args.kwargs["token"] is False
    assert sdk.CrossEncoder.call_args.kwargs["model_kwargs"] == {"use_safetensors": True}
    assert model.rank("question", CHUNKS) == tuple(sorted(CHUNKS, key=lambda c: c.chunk_id))
    assert len(model._vectors) == 2
    assert all(
        vector.base is None and vector.nbytes == 384 * 4 for vector in model._vectors.values()
    )
    model.rank("question", CHUNKS[1:])
    assert embedding.encode_document.call_count == 1
    assert embedding.encode_query.call_count == 2
    changed = CHUNKS[1].model_copy(update={"text": "different evidence"})
    assert model.rank("question", (changed,)) == (changed,)
    assert embedding.encode_document.call_count == 2 and len(model._vectors) == 2
    assert model.rank("question", ()) == model.rerank("question", ()) == ()
    assert model.rerank("question", CHUNKS, 1) == (min(CHUNKS, key=lambda c: c.chunk_id),)
    model.close()
    assert not model._vectors and model.embedding is None
    with pytest.raises(ServiceError, match="model_unavailable"):
        model.rank("question", CHUNKS)


def test_model_busy_failure_and_lock_recovery(numeric_models: tuple) -> None:
    """Concurrent work fails promptly; failed inference releases ownership for the next request."""
    _, embedding, reranker, _ = numeric_models
    model = LocalNeuralRanker(Path("unused"))
    model._lock.acquire()
    try:
        with pytest.raises(ServiceError, match="model_busy"):
            model.rank("question", CHUNKS)
    finally:
        model._lock.release()
    embedding.encode_document.side_effect = RuntimeError("private model details")
    with pytest.raises(ServiceError, match="model_unavailable") as error:
        model.rank("question", CHUNKS)
    assert "private" not in error.value.message
    assert not model._lock.locked()
    reranker.predict.side_effect = OSError("private weights path")
    with pytest.raises(ServiceError, match="model_unavailable"):
        model.rerank("question", CHUNKS)
    assert not model._lock.locked()


@pytest.mark.parametrize("stage", ["document", "query", "reranker"])
@pytest.mark.parametrize("fault", ["shape", "nonfinite"])
def test_invalid_numeric_outputs_fail(numeric_models: tuple, stage: str, fault: str) -> None:
    """NaNs and wrong tensor dimensions cannot yield apparently valid evidence rankings."""
    _, embedding, reranker, np = numeric_models
    model = LocalNeuralRanker(Path("unused"))
    shape = {"document": (len(CHUNKS), 384), "query": (1, 384), "reranker": (len(CHUNKS),)}[stage]
    value = np.full((1,) if fault == "shape" else shape, np.nan if fault == "nonfinite" else 1)
    target = {
        "document": embedding.encode_document,
        "query": embedding.encode_query,
        "reranker": reranker.predict,
    }[stage]
    target.side_effect = None
    target.return_value = value
    call = model.rerank if stage == "reranker" else model.rank
    with pytest.raises(ServiceError, match="invalid_model_output"):
        call("question", CHUNKS)


def test_model_input_and_constructor_limits(numeric_models: tuple) -> None:
    """Reject oversized scope or text before allocating model tensors or invoking SDKs."""
    for count in (0, 4097):
        with pytest.raises(ValueError):
            LocalNeuralRanker(Path("unused"), cache_entries=count)
    cases = [
        ("question", CHUNKS, 0, 512),
        ("question", CHUNKS, 101, 512),
        ("x" * 2001, CHUNKS, 10, 512),
        ("question", CHUNKS * 171, 10, 512),
        ("question", CHUNKS * 14, 10, 40),
        ("question", (CHUNKS[0].model_copy(update={"text": "x" * 16001}),), 10, 512),
        ("question", (CHUNKS[0].model_copy(update={"text": "x" * 16000}),) * 63, 10, 512),
    ]
    for values in cases:
        with pytest.raises(ServiceError, match="model_input_limit"):
            input_budget(*values)
    sdk = numeric_models[0]
    sdk.SentenceTransformer.side_effect = ImportError("optional SDK absent")
    with pytest.raises(ServiceError, match="model_unavailable"):
        LocalNeuralRanker(Path("unused"))


def test_hybrid_lifespan_and_cache_revision(
    numeric_models: tuple, monkeypatch: pytest.MonkeyPatch, neural_traces: Path
) -> None:
    """Runtime composition binds model revision into cache identity and closes resources."""
    from creditlens import runtime

    backend = MemoryBytes()
    backend.close = Mock()
    monkeypatch.setattr(runtime, "RedisBytes", Mock(return_value=backend))
    config = Settings(
        database_url="sqlite:///:memory:",
        retrieval_mode="hybrid",
        local_model_directory="unused",
        redis_url="redis://127.0.0.1:1",
        cache_signing_key="k" * 32,
        telemetry_enabled=True,
        trace_file=str(neural_traces),
    )
    with TestClient(create_app(config)) as client:
        provider = client.app.state.workflow.provider
        assert "fixture-revision" in provider.provider_revision
        body = {
            "borrower_id": "borrower-001",
            "question": "calculate DSCR",
            "effective_at": "2026-09-11",
        }
        first = client.post("/api/v1/query", json=body)
        assert first.status_code == 200
        assert client.post("/api/v1/query", json=body).json()["cache_hit"] is True
        models = provider.provider.provider.ranker.__self__
        assert models.telemetry is client.app.state.telemetry
        assert 'stage="neural.embed_documents"' in models.telemetry.render().decode()
    assert models._closed and models.embedding is None
    backend.close.assert_called_once()


def test_model_settings_and_startup_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Partial config and absent model files cannot silently start a lexical workflow."""
    for values in ({"retrieval_mode": "hybrid"}, {"local_model_directory": "unused"}):
        with pytest.raises(ValidationError):
            Settings(**values)
    values = production_settings().model_dump()
    values.update(retrieval_mode="hybrid", local_model_directory="unused")
    with pytest.raises(ValidationError, match="demo mode only"):
        Settings(**values)
    with (
        pytest.raises(ServiceError, match="model_bundle_invalid"),
        TestClient(
            create_app(
                Settings(
                    database_url="sqlite:///:memory:",
                    retrieval_mode="hybrid",
                    local_model_directory="path-that-does-not-exist",
                )
            )
        ),
    ):
        pass


def test_model_shutdown_survives_cache_close_failure(
    numeric_models: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing optional transport cleanup must still release models and retained evidence."""
    from creditlens import runtime

    backend = MemoryBytes()
    backend.close = Mock(side_effect=RuntimeError("cache close failed"))
    monkeypatch.setattr(runtime, "RedisBytes", Mock(return_value=backend))
    config = Settings(
        database_url="sqlite:///:memory:",
        retrieval_mode="hybrid",
        local_model_directory="unused",
        redis_url="redis://127.0.0.1:1",
        cache_signing_key="k" * 32,
    )
    with (
        pytest.raises(RuntimeError, match="cache close failed"),
        TestClient(create_app(config)) as client,
    ):
        models = client.app.state.workflow.provider.provider.provider.ranker.__self__
    assert models._closed and not models._vectors
