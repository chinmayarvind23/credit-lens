"""Bounded, digest-pinned local-model inference for the governed production RAG workflow."""

import json
import re
from collections.abc import Callable
from threading import Lock
from time import monotonic
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError

from creditlens.domain import Packet, QueryRequest, Synthesis
from creditlens.errors import ServiceError
from creditlens.generation_contract import (
    GENERATION_OPTIONS,
    PROMPT_VERSION,
    REFUSALS,
    GenerationDraft,
    generation_messages,
    generation_revision,
    response_schema,
    supported_statements,
    validate_synthesis,
)
from creditlens.generation_transport import StreamingClient
from creditlens.opensearch_provider import validate_search_url


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys rather than accepting an ambiguous model/server contract."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate response key")
        result[key] = value
    return result


class OllamaGenerator:
    """Own no model process; inference uses an existing verified model with no cloud fallback."""

    def __init__(
        self,
        endpoint: str,
        model: str,
        digest: str,
        client: StreamingClient,
        *,
        token: SecretStr | None = None,
        timeout_seconds: float = 120,
    ) -> None:
        """Allow plaintext only on literal loopback; remote operators must use authenticated TLS."""
        validate_search_url(endpoint, "ollama", True)
        if not re.fullmatch(r"[a-z0-9._-]+:[a-z0-9._-]+", model) or "cloud" in model:
            raise ValueError("Expected an installed local model tag")
        if not re.fullmatch(r"[a-f0-9]{64}", digest) or not 1 <= timeout_seconds <= 180:
            raise ValueError("Expected an immutable digest and bounded generation timeout")
        token = token or SecretStr("")
        if endpoint.startswith("https:") != bool(token.get_secret_value()):
            raise ValueError("Remote generation requires TLS and a token; loopback uses no token")
        self.endpoint, self.model, self.digest = endpoint.rstrip("/"), model, digest
        self.client, self.token, self.timeout_seconds = client, token, timeout_seconds
        self.revision = generation_revision(model, digest)
        self._lock = Lock()

    def request(self, path: str, body: dict[str, Any] | None, deadline: float) -> dict[str, Any]:
        """Bound total elapsed time and response bytes; redact provider details on every failure."""
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if self.token.get_secret_value():
            headers["Authorization"] = f"Bearer {self.token.get_secret_value()}"
        try:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise ValueError("Generation deadline exceeded")
            with self.client.stream(
                "GET" if body is None else "POST",
                self.endpoint + path,
                json=body,
                headers=headers,
                timeout=remaining,
                follow_redirects=False,
            ) as response:
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise ValueError("Encoded model response")
                payload = bytearray()
                for block in response.iter_bytes():
                    payload.extend(block)
                    if len(payload) > 1_000_000 or monotonic() > deadline:
                        raise ValueError("Model response exceeds budget")
            result = json.loads(payload, object_pairs_hook=unique_object)
            if not isinstance(result, dict):
                raise ValueError("Invalid model response")
            return result
        except (httpx.HTTPError, ValueError, RecursionError) as error:
            raise ServiceError(
                "generation_unavailable", "Answer generation is unavailable"
            ) from error

    def verify_model(self, deadline: float) -> bool:
        """Check digest and local GGUF capability before and after inference, never pull a model."""
        models = self.request("/api/tags", None, deadline).get("models")
        if not isinstance(models, list) or not any(
            isinstance(model, dict)
            and model.get("name") == self.model
            and model.get("digest") == self.digest
            for model in models
        ):
            raise ServiceError(
                "generation_model_changed", "Configured generation model is unavailable"
            )
        details = self.request("/api/show", {"model": self.model}, deadline)
        metadata, capabilities = details.get("details"), details.get("capabilities")
        if (
            details.get("remote_host")
            or details.get("remote_model")
            or not isinstance(metadata, dict)
            or metadata.get("format") != "gguf"
            or not isinstance(capabilities, list)
            or "completion" not in capabilities
        ):
            raise ServiceError(
                "generation_model_invalid", "Configured generation model is unavailable"
            )
        return "thinking" in capabilities

    def check_ready(self) -> None:
        """Check model identity without generating content or sending borrower evidence."""
        self.verify_model(monotonic() + min(10, self.timeout_seconds))

    def synthesize(
        self, query: QueryRequest, packet: Packet, authorize: Callable[[], None]
    ) -> Synthesis:
        """Admit one request, check authority at exposure, and reject incomplete output."""
        if packet.abstained or not packet.evidence:
            raise ServiceError("generation_withheld", "Evidence does not support synthesis")
        messages = generation_messages(query, packet)
        if not self._lock.acquire(blocking=False):
            raise ServiceError("generation_busy", "Answer generation is busy; retry the request")
        try:
            deadline = monotonic() + self.timeout_seconds
            thinking = self.verify_model(deadline)
            authorize()
            result = self.request(
                "/api/chat",
                {
                    "model": self.model,
                    "messages": messages,
                    "format": response_schema(packet),
                    "stream": False,
                    **({"think": False} if thinking else {}),
                    "keep_alive": "1m",
                    "options": dict(GENERATION_OPTIONS),
                },
                deadline,
            )
            self.verify_model(deadline)
            authorize()
            return self.decode(result, packet)
        finally:
            self._lock.release()

    def decode(self, result: dict[str, Any], packet: Packet) -> Synthesis:
        """Only complete assistant JSON can become separately cited model interpretation."""
        try:
            message = result["message"]
            if (
                result.get("model") != self.model
                or result.get("done") is not True
                or result.get("done_reason") != "stop"
                or not isinstance(message, dict)
                or message.get("role") != "assistant"
                or message.get("tool_calls")
                or message.get("thinking")
                or not isinstance(message.get("content"), str)
            ):
                raise ValueError("Incomplete or unexpected model output")
            content = json.loads(message["content"], object_pairs_hook=unique_object)
            draft = GenerationDraft.model_validate_json(json.dumps(content), strict=True)
            synthesis = Synthesis(
                status=draft.status,
                statements=supported_statements(draft, packet),
                refusal_reason=REFUSALS.get(draft.refusal_category, ""),
                refusal_category=draft.refusal_category,
                model=self.model,
                model_digest=self.digest,
                prompt_version=PROMPT_VERSION,
                prompt_tokens=result["prompt_eval_count"],
                output_tokens=result["eval_count"],
            )
        except (KeyError, TypeError, ValueError, ValidationError, RecursionError) as error:
            raise ServiceError(
                "invalid_generation", "Generated answer could not be validated"
            ) from error
        validate_synthesis(packet.model_copy(update={"synthesis": synthesis}))
        return synthesis
