"""Retain GEval schema outputs while reusing the pinned local-only judge transport."""

from pathlib import Path
from typing import Any

from local_judge import LocalJudge
from pydantic import BaseModel


class AdviceJudge(LocalJudge):
    """The runner owns one bounded judge and records every actual schema response."""

    def __init__(self, model: str, digest: str, journal: Path) -> None:
        """Permit at most one call per case in the existing 24-case batch limit."""
        super().__init__(model, digest, journal, calls=24)
        self.outputs: list[dict] = []

    def generate(self, prompt: str, schema: type[BaseModel] | None = None) -> Any:
        """Fail untyped generation and preserve parsed outputs before GEval transforms scores."""
        if schema is None:
            raise ValueError("Advice evaluation requires structured generation")
        result = super().generate(prompt, schema)
        self.outputs.append({"schema": schema.__name__, "output": result.model_dump()})
        return result
