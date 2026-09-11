"""Schema-aware DeepEval judge restricted to an existing, digest-pinned local Ollama model."""

import asyncio
import json
import os
import re
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx
from pydantic import BaseModel

# These must precede DeepEval imports; evaluation never inherits hosted credentials or dotenv.
os.environ.update(
    DEEPEVAL_TELEMETRY_OPT_OUT="YES",
    DEEPEVAL_DISABLE_DOTENV="1",
    DEEPEVAL_DISABLE_LEGACY_KEYFILE="1",
    DEEPEVAL_FILE_SYSTEM="READ_ONLY",
    CONFIDENT_API_KEY="",
)
from deepeval.models import DeepEvalBaseLLM  # noqa: E402


class LocalJudge(DeepEvalBaseLLM):  # type: ignore[no-untyped-call]  # upstream subclass hook
    """Never download, launch, or select a hosted model; each call checks local model identity."""

    def __init__(self, model: str, digest: str, journal: Path, *, calls: int = 24) -> None:
        """Journal judgments and limit calls so failures remain visible and work stays bounded."""
        if not re.fullmatch(r"[a-z0-9._-]+:[a-z0-9._-]+", model) or "cloud" in model:
            raise ValueError("An explicit local model tag is required")
        if not re.fullmatch(r"[a-f0-9]{64}", digest) or not 1 <= calls <= 100:
            raise ValueError("A SHA256 digest and bounded call budget are required")
        self.tag, self.digest, self.journal, self.remaining = model, digest, journal, calls
        self.client = httpx.Client(
            base_url="http://127.0.0.1:11434",
            timeout=120,
            trust_env=False,
            follow_redirects=False,
        )
        super().__init__(model=model)

    def load_model(self) -> "LocalJudge":
        """The owner already runs Ollama; model loading is delegated only to its local API."""
        return self

    def get_model_name(self) -> str:
        """Bind reported judge identity to content, rather than a mutable tag alone."""
        return f"ollama:{self.tag}@sha256:{self.digest}"

    def request(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Bound response bytes and prohibit proxy/redirect routing before JSON decoding."""
        with self.client.stream("GET" if body is None else "POST", path, json=body) as response:
            response.raise_for_status()
            content = bytearray()
            for block in response.iter_bytes():
                content.extend(block)
                if len(content) > 1_000_000:
                    raise ValueError("Local judge response exceeds one megabyte")
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("Local judge response must be an object")
        return result

    def verify_model(self) -> None:
        """Refuse a changed tag or remote-model descriptor before asking for inference."""
        models = self.request("/api/tags")["models"]
        if not any(m["name"] == self.tag and m["digest"] == self.digest for m in models):
            raise ValueError("Local judge digest does not match")
        details = self.request("/api/show", {"model": self.tag})
        if details.get("remote_host") or details.get("remote_model"):
            raise ValueError("Remote judge routing is forbidden")
        if details.get("details", {}).get("format") != "gguf":
            raise ValueError("Expected an installed local GGUF model")

    def generate(self, prompt: str, schema: type[BaseModel] | None = None) -> Any:
        """Use deterministic bounded JSON generation and reject incomplete or malformed outputs."""
        if self.remaining <= 0 or len(prompt.encode()) > 32_000:
            raise ValueError("Judge call or input budget exhausted")
        self.remaining -= 1
        self.verify_model()
        started = perf_counter()
        result = self.request(
            "/api/chat",
            {
                "model": self.tag,
                "stream": False,
                "keep_alive": "1m",
                "messages": [{"role": "user", "content": prompt}],
                "format": schema.model_json_schema() if schema else "json",
                "options": {
                    "temperature": 0,
                    "seed": 0,
                    "num_predict": 2048,
                    "num_ctx": 8192,
                    "num_thread": 4,
                },
            },
        )
        with self.journal.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "prompt": prompt,
                        "response": result,
                        "elapsed_seconds": perf_counter() - started,
                    }
                )
                + "\n"
            )
        if result.get("done") is not True or result.get("done_reason") != "stop":
            raise ValueError("Judge generation was incomplete")
        self.verify_model()
        content = result["message"]["content"]
        return schema.model_validate_json(content) if schema else content

    async def a_generate(self, prompt: str, schema: type[BaseModel] | None = None) -> Any:
        """Keep interface compatibility; the runner deliberately executes metrics sequentially."""
        return await asyncio.to_thread(self.generate, prompt, schema)

    def close(self) -> None:
        """Release the HTTP client without stopping the owner's shared Ollama service."""
        self.client.close()
