"""Candidate experiment checks protect score validity and the pre-model scope boundary."""

from pathlib import Path

import pytest

from creditlens.corpus import build_demo_pages
from creditlens.evaluation import GoldCase
from creditlens.retrieval import EvidenceCatalog, chunk_page
from scripts.benchmark_candidates import allowed_chunks, rank_scores


def test_prefix_blocks_high_scoring_outsiders_and_breaks_ties() -> None:
    """A strong score from another prefix must not expand a variant's candidate budget."""
    chunks = tuple(chunk_page(page)[0] for page in build_demo_pages()[:3])
    scores = {c.chunk_id: 1.0 for c in chunks}
    scores[chunks[2].chunk_id] = 999.0
    result = rank_scores(chunks[:2], scores)
    assert result == tuple(sorted(chunks[:2], key=lambda c: c.chunk_id))
    assert chunks[2] not in result


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_scores_fail(value: float) -> None:
    """Malformed model scores must invalidate an experiment rather than produce rankings."""
    chunk = chunk_page(build_demo_pages()[0])[0]
    with pytest.raises(ValueError, match="finite"):
        rank_scores((chunk,), {chunk.chunk_id: value})


def test_missing_scores_and_duplicate_candidates_fail() -> None:
    """Partial inference and duplicate identities cannot inflate retrieval statistics."""
    chunk = chunk_page(build_demo_pages()[0])[0]
    with pytest.raises(ValueError, match="present"):
        rank_scores((chunk,), {})
    with pytest.raises(ValueError, match="Duplicate"):
        rank_scores((chunk, chunk), {chunk.chunk_id: 1.0})


def test_scope_denials_and_forged_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Authored auditing catches a catalog leak before the experiment may send text to models."""
    cases = [
        GoldCase.model_validate_json(line)
        for line in Path("evals/gold_cases.jsonl").read_text().splitlines()
    ]
    catalog = EvidenceCatalog(build_demo_pages())
    case = cases[0]
    chunks, revision = allowed_chunks(catalog, case)
    assert chunks and revision
    denied = next(c for c in cases if c.expected_behavior == "deny")
    assert allowed_chunks(catalog, denied) == ((), 0)
    forged = chunks[0].model_copy(update={"tenant_id": "other-bank"})
    # Deliberately broken catalog tests the independent auditor, not model behavior.
    monkeypatch.setattr(catalog, "snapshot", lambda *_args: ((forged,), revision))
    with pytest.raises(ValueError, match="Unauthorized"):
        allowed_chunks(catalog, case)
