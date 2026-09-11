"""The composed evaluator must retain authored scopes and fail on unexpected provider errors."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from creditlens.corpus import build_demo_pages
from creditlens.errors import ServiceError
from creditlens.evaluation import GoldCase
from creditlens.evaluation_outcomes import expected_path
from creditlens.local_search import LocalSearchProvider
from creditlens.retrieval import EvidenceCatalog
from creditlens.storage import GrantStore, open_database
from scripts.benchmark_cache_http import serve
from scripts.benchmark_neural import run_case


def test_composed_fixture_scoring_and_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the real lexical scorer as an explicit control for the new evaluator's mechanics."""
    cases = [
        GoldCase.model_validate_json(line)
        for line in Path("evals/gold_cases.jsonl").read_text().splitlines()
    ]
    engine = open_database("sqlite:///:memory:")
    try:
        store = GrantStore(engine)
        provider = LocalSearchProvider(EvidenceCatalog(build_demo_pages()), store)
        row = run_case(cases[0], provider, store)
        assert row["score"]["eligible"] and row["outcome"]["path_pass"]
        assert not row["ranking_violations"]
        denied = run_case(next(c for c in cases if c.expected_behavior == "deny"), provider, store)
        assert denied["denied_before_ranking"] and denied["outcome"]["fixture_pass"]
        monkeypatch.setattr(
            provider, "search", Mock(side_effect=ServiceError("model_unavailable", "Unavailable"))
        )
        with pytest.raises(ServiceError, match="model_unavailable"):
            run_case(cases[0], provider, store)
    finally:
        engine.dispose()


def test_evaluation_path_and_startup_bounds() -> None:
    """Unknown trace definitions and unreasonable startup waits cannot silently pass checks."""
    from creditlens.settings import Settings

    with pytest.raises(ValueError, match="Unknown"):
        expected_path([], "invented-stage")
    for timeout in (0, 121):
        with (
            pytest.raises(ValueError, match="Startup timeout"),
            serve(Settings(), startup_timeout=timeout),
        ):
            pass
