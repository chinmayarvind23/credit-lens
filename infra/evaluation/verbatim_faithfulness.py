"""Preserve each whole field as one support unit; this is not atomic-claim faithfulness."""

# ruff: noqa: I001 -- Privacy initialization must precede the SDK import.

# Initialize evaluation privacy before importing the SDK, including transitive tracing.
from ragas_judge import RagasJudge  # noqa: F401

from ragas.metrics.collections import Faithfulness


class VerbatimFieldSupport(Faithfulness):
    """Retain RAGAS NLI and scoring while removing generative extraction from the metric."""

    async def _create_statements(self, question: str, response: str) -> list[str]:
        """A single exact field prevents extraction from adding, omitting or rephrasing facts."""
        return [response]
