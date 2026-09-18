"""Exercise transport and admission boundaries independently of model answer quality."""

from time import monotonic

import httpx
import pytest
from pydantic import SecretStr

from creditlens.errors import ServiceError
from creditlens.ollama_generation import OllamaGenerator
from tests.test_generation_safety import DIGEST, MODEL, QUESTION
from tests.test_generation_safety import backend as backend
from tests.test_generation_safety import evidence as evidence


@pytest.mark.parametrize(
    "changes",
    [
        {"model": "invalid tag"},
        {"model": "remote-cloud:8b"},
        {"digest": "not-a-digest"},
        {"timeout_seconds": 0},
        {"timeout_seconds": 181},
    ],
)
def test_invalid_model_identity_or_deadline_fails_before_io(changes) -> None:
    """Reject unsafe startup configuration before any connection can expose evidence."""
    calls = []

    def transport(request):
        """Record accidental transport access; validation must fail before this boundary."""
        calls.append(request)
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        values = dict(endpoint="http://127.0.0.1:11434", model=MODEL, digest=DIGEST, client=client)
        with pytest.raises(ValueError):
            OllamaGenerator(**(values | changes))
    assert calls == []


def test_authenticated_remote_transport_preserves_header_and_identity() -> None:
    """A configured TLS gateway receives its token only on the pinned explicit origin."""
    calls = []

    def transport(request):
        """Observe the outbound contract without making a real network request."""
        calls.append(request)
        return httpx.Response(200, json={"models": []})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        generator = OllamaGenerator(
            "https://model.example.invalid",
            MODEL,
            DIGEST,
            client,
            token=SecretStr("synthetic-gateway-token"),
        )
        assert generator.request("/api/tags", None, monotonic() + 5) == {"models": []}
    assert len(calls) == 1
    assert calls[0].url == "https://model.example.invalid/api/tags"
    assert calls[0].headers["authorization"] == "Bearer synthetic-gateway-token"
    assert calls[0].headers["accept-encoding"] == "identity"


def test_expired_request_deadline_prevents_transport(backend) -> None:
    """An already exhausted shared budget must not start another HTTP request."""
    with pytest.raises(ServiceError, match="generation_unavailable"):
        backend.generator.request("/api/tags", None, monotonic() - 1)
    assert backend.calls == []


@pytest.mark.parametrize("reply", ["encoded", "non-object"])
def test_unexpected_response_representation_is_rejected(reply: str) -> None:
    """Reject encoded bodies and JSON arrays before they can enter model-identity decoding."""

    def transport(request):
        """Use a deliberately unsupported representation from the trusted endpoint boundary."""
        if reply == "encoded":
            return httpx.Response(200, content=b"{}", headers={"content-encoding": "custom"})
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        generator = OllamaGenerator("http://127.0.0.1:11434", MODEL, DIGEST, client)
        with pytest.raises(ServiceError, match="generation_unavailable"):
            generator.request("/api/tags", None, monotonic() + 5)


@pytest.mark.parametrize("change", [{"abstained": True}, {"evidence": ()}])
def test_missing_or_abstained_evidence_never_enters_generation(evidence, backend, change) -> None:
    """Direct generator consumers cannot bypass workflow-level abstention or empty context."""
    with pytest.raises(ServiceError, match="generation_withheld"):
        backend.generator.synthesize(
            QUESTION, evidence.packet.model_copy(update=change), lambda: None
        )
    assert backend.calls == []


def test_busy_model_rejects_without_io_then_recovers(evidence, backend) -> None:
    """Concurrent admission fails immediately while a later request can still use the model."""
    assert backend.generator._lock.acquire(blocking=False)
    try:
        with pytest.raises(ServiceError, match="generation_busy"):
            backend.generator.synthesize(QUESTION, evidence.packet, lambda: None)
        assert backend.calls == []
    finally:
        backend.generator._lock.release()
    result = backend.generator.synthesize(QUESTION, evidence.packet, lambda: None)
    assert result.status == "answered" and len(backend.chat_calls()) == 1
