"""Adapt RAGAS structured generation to the existing bounded, local-only judge transport."""

import os
from typing import TypeVar

# Set these before either SDK import, including LangChain's transitive tracing integration.
os.environ.update(
    RAGAS_DO_NOT_TRACK="true",
    HF_HUB_DISABLE_TELEMETRY="1",
    HF_HUB_OFFLINE="1",
    LANGCHAIN_TRACING="false",
    LANGCHAIN_TRACING_V2="false",
    LANGSMITH_TRACING="false",
)
from local_judge import LocalJudge  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from ragas.llms.base import InstructorBaseRagasLLM  # noqa: E402

Response = TypeVar("Response", bound=BaseModel)


class RagasJudge(InstructorBaseRagasLLM):
    """Keep library prompts unchanged while preserving every parsed response for inspection."""

    def __init__(self, transport: LocalJudge) -> None:
        """Borrow the transport; the runner owns its bounded lifetime and private journal."""
        self.transport = transport
        self.outputs: list[dict] = []

    def generate(self, prompt: str, response_model: type[Response]) -> Response:
        """Retain the actual library schema and output, with no hosted fallback or output repair."""
        result = self.transport.generate(prompt, response_model)
        self.outputs.append({"schema": response_model.__name__, "output": result.model_dump()})
        return result

    async def agenerate(self, prompt: str, response_model: type[Response]) -> Response:
        """Run synchronously inside the sequential evaluation loop to avoid concurrent inference."""
        return self.generate(prompt, response_model)
