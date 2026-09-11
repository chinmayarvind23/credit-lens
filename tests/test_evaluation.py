"""Known-answer metric math and security probes keep the evaluator itself accountable."""

import json
import math
from dataclasses import asdict
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from creditlens.corpus import borrower_pages, policy_pages
from creditlens.errors import ServiceError
from creditlens.evaluation import (
    GoldCase,
    RankingScore,
    evaluate,
    gate_results,
    percentile,
    rank_case,
    read_inputs,
    scope_violations,
    score_ranking,
    summarize_ranking,
)
from creditlens.evaluation_outcomes import add_outcomes, expected_path, failed_outcome
from creditlens.retrieval import EvidenceCatalog

A, B, C, X = ((name, "v1", 1) for name in ("a", "b", "c", "irrelevant"))


def gold_cases() -> list[GoldCase]:
    """Read frozen declarations rather than inventing a second principal contract."""
    path = Path(__file__).resolve().parents[1] / "evals" / "gold_cases.jsonl"
    return [GoldCase.model_validate_json(line) for line in path.read_text().splitlines()]


def test_graded_dcg_and_duplicate_page_math() -> None:
    """Duplicate chunks cannot consume another rank or earn another relevance hit."""
    result = score_ranking([X, B, B, A, C], {A: 3, B: 2, C: 1}, k=3)
    assert result.recall == pytest.approx(2 / 3)
    assert result.reciprocal_rank == 0.5
    expected = (3 / math.log2(3) + 7 / math.log2(4)) / (
        7 / math.log2(2) + 3 / math.log2(3) + 1 / math.log2(4)
    )
    assert result.ndcg == pytest.approx(expected)
    assert score_ranking([A, B, C], {A: 3, B: 2, C: 1}).ndcg == 1


def test_empty_qrels_and_failed_retrieval_denominators() -> None:
    """No-evidence cases stay undefined while eligible retrieval failures receive zero credit."""
    assert score_ranking([A], {}) == RankingScore(False, 0, 0, None, None, None)
    assert score_ranking([], {A: 1}) == RankingScore(True, 0, 1, 0, 0, 0)
    assert score_ranking([A], {A: 0}).eligible is False
    with pytest.raises(ValueError, match="cutoff"):
        score_ranking([], {}, k=0)
    with pytest.raises(ValueError, match="grades"):
        score_ranking([A], {A: -1})


def test_macro_micro_and_excluded_denominators() -> None:
    """Macro weights questions and micro weights relevant pages, excluding no-qrel cases."""
    scores = [
        score_ranking([A], {A: 1}),
        score_ranking([A], {A: 1, B: 1, C: 1}),
        score_ranking([], {}),
    ]
    records = [
        {"score": asdict(score), "ranking_latency_ms": index + 1}
        for index, score in enumerate(scores)
    ]
    summary = summarize_ranking(records)
    assert summary["eligible_cases"] == 2
    assert summary["excluded_no_qrel_cases"] == 1
    assert summary["macro_recall_at_10"] == pytest.approx(2 / 3)
    assert summary["micro_recall_at_10"] == 0.5
    assert summary["p95_ranking_latency_ms"] == 3
    assert percentile([], 0.95) is None


def test_independent_scope_audit_checks_boundaries() -> None:
    """Production authorization bugs cannot silently pass the independently defined scope check."""
    case, page = gold_cases()[0], borrower_pages(1)[0]
    assert not scope_violations(page, case)
    changes = (
        {"tenant_id": "other-bank"},
        {"borrower_id": "borrower-002"},
        {"acl_groups": ("credit-officer",)},
        {"valid_from": date(2027, 1, 1)},
        {"valid_to": date(2026, 2, 1)},
        {"extraction_confidence": 0.5},
    )
    for change in changes:
        assert scope_violations(page.model_copy(update=change), case)
    assert scope_violations(page, case.model_copy(update={"borrower_grants": ()}))


def test_catalog_denies_out_of_grant_before_scoring() -> None:
    """Probe actual catalog state using a frozen cross-tenant negative case."""
    case = next(case for case in gold_cases() if case.expected_behavior == "deny")
    record = rank_case(EvidenceCatalog(policy_pages() + borrower_pages(151)), case)
    assert record["denied_before_ranking"]
    assert record["candidate_count"] == 0
    assert record["ranked_chunk_ids"] == []
    assert record["score"]["recall"] is None


def test_inputs_reject_text_tampering() -> None:
    """Stored hashes bind the extracted evidence that is actually evaluated."""
    with TemporaryDirectory(prefix="creditlens-eval-") as directory:
        gold, pages = Path(directory) / "gold.jsonl", Path(directory) / "pages.jsonl"
        gold.write_text(gold_cases()[0].model_dump_json() + "\n", encoding="utf-8")
        page = next(
            page for page in policy_pages() if page.document_version == "v2" and page.page == 1
        )
        pages.write_text(page.model_dump_json() + "\n", encoding="utf-8")
        assert len(read_inputs(gold, pages)[0]) == 1
        pages.write_text(
            page.model_copy(update={"text": "tampered"}).model_dump_json(), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="hash mismatch"):
            read_inputs(gold, pages)


def gate_fixture() -> tuple[dict[str, Any], dict[str, Any]]:
    """A compact comparable run exposes gate boundaries without relying on live scores."""
    current = {
        "gold_sha256": "gold",
        "pages_sha256": "pages",
        "metric_contract": "v1",
        "execution": {"unauthorized_candidates": 0, "unauthorized_ranked": 0, "ranking_errors": 0},
        "outcomes": {"status": "not_run"},
        "ranking": {
            "scheduled_cases": 240,
            "eligible_cases": 220,
            "macro_recall_at_10": 1,
            "ndcg_at_10": 1,
            "mrr_at_10": 1,
        },
    }
    path = Path(__file__).resolve().parents[1] / "evals" / "gates.json"
    return current, json.loads(path.read_text(encoding="utf-8"))


def test_gate_security_and_incompatible_runs_fail() -> None:
    """Unauthorized items veto quality; missing audit fields cannot masquerade as zero."""
    current, policy = gate_fixture()
    baseline = json.loads(json.dumps(current))
    assert not gate_results(current, baseline, policy)
    current["execution"]["unauthorized_candidates"] = 1
    assert "unauthorized_candidates" in gate_results(current, baseline, policy)
    del current["execution"]["unauthorized_candidates"]
    assert "unauthorized_candidates" in gate_results(current, baseline, policy)
    current["execution"]["unauthorized_candidates"] = 0
    current["gold_sha256"] = "different"
    assert "incompatible_gold_sha256" in gate_results(current, baseline, policy)


def test_gate_rejects_five_percent_regression_boundary() -> None:
    """The frozen relative gate applies at five percent without an invented absolute score floor."""
    current, policy = gate_fixture()
    baseline = json.loads(json.dumps(current))
    current["ranking"]["macro_recall_at_10"] = 0.95
    assert "regression_macro_recall_at_10" in gate_results(current, baseline, policy)
    current["ranking"]["macro_recall_at_10"] = 0.951
    assert not gate_results(current, baseline, policy)
    baseline["ranking"]["macro_recall_at_10"] = 0.7
    current["ranking"]["macro_recall_at_10"] = 0.665
    assert "regression_macro_recall_at_10" in gate_results(current, baseline, policy)


def test_gate_rejects_missing_outcome_fields_and_removed_scope() -> None:
    """A challenger cannot skip the workflow checks present in its baseline."""
    current, policy = gate_fixture()
    current["outcomes"] = {"status": "deterministic_fixture_only"}
    failures = gate_results(current, None, policy)
    assert "workflow_unauthorized_context" in failures
    assert "workflow_path_failures" in failures
    baseline = json.loads(json.dumps(current))
    current["outcomes"] = {"status": "not_run"}
    assert "incompatible_outcome_scope" in gate_results(current, baseline, policy)


def test_denial_requires_observed_absence_of_ranking_context_and_writes() -> None:
    """A403 after retrieval is a failed path even if the final HTTP status looks correct."""
    case = next(case for case in gold_cases() if case.expected_behavior == "deny")
    error = ServiceError("access_denied", "Denied", 403)
    observed = {"ranking_calls": 0, "context_calls": 0, "audit_writes": 0}
    assert failed_outcome(error, case, observed)["path_pass"]
    for key in observed:
        assert not failed_outcome(error, case, observed | {key: 1})["path_pass"]
    assert not failed_outcome(error, case, {})["path_pass"]


def test_workflow_outcomes_probe_real_sql_state_and_denial_path() -> None:
    """Run a supported query and denial through the real workflow and inspect persisted audit."""
    available = gold_cases()
    cases = (available[0], next(case for case in available if case.expected_behavior == "deny"))
    catalog = EvidenceCatalog(policy_pages() + borrower_pages(1) + borrower_pages(151))
    records = [rank_case(catalog, case) for case in cases]
    with TemporaryDirectory(prefix="creditlens-eval-outcome-") as directory:
        output = Path(directory)
        add_outcomes(records, cases, catalog, output)
        assert records[0]["outcome"]["audit_persisted"]
        assert records[0]["outcome"]["path_pass"]
        assert records[1]["outcome"]["denial_path_observation"] == {
            "ranking_calls": 0,
            "context_calls": 0,
            "audit_writes": 0,
        }
        assert records[1]["outcome"]["path_pass"]
        assert len(json.loads((output / "audit-events.json").read_text())) == 1


def test_gold_path_rejects_retrieval_before_authorization() -> None:
    """Required stage ordering is independent of whether the final packet happens to look right."""
    stages = [
        "auth.resolve_current_grant",
        "authorization.filter_before_retrieval",
        "retrieval.local_bm25",
        "citation.validate_exact_extracts",
        "authorization.recheck",
        "audit.persist",
    ]
    assert expected_path(stages)
    stages[1], stages[2] = stages[2], stages[1]
    assert not expected_path(stages)
    assert not expected_path(stages[:-1])


def test_run_artifacts_bind_inputs_and_preserve_unmeasured_semantic_targets() -> None:
    """Exercise the writer so source manifests and denominators match saved case records."""
    cases = gold_cases()
    selected = (cases[0], next(case for case in cases if case.expected_behavior == "deny"))
    pages = policy_pages() + borrower_pages(151)
    repo = Path(__file__).resolve().parents[1]
    with TemporaryDirectory(prefix="creditlens-eval-run-") as directory:
        folder = Path(directory)
        gold_path, pages_path = folder / "gold.jsonl", folder / "pages.jsonl"
        gold_path.write_text(
            "\n".join(case.model_dump_json() for case in selected), encoding="utf-8"
        )
        pages_path.write_text("\n".join(page.model_dump_json() for page in pages), encoding="utf-8")
        result = evaluate(gold_path, pages_path, folder / "run", repo, outcomes=True)
        assert result["ranking"]["scheduled_cases"] == 2
        assert result["ranking"]["eligible_cases"] == 1
        assert result["targets"]["grounded_answer_pass_rate"]["measured"] is None
        manifest = json.loads((folder / "run" / "manifest.json").read_text())
        assert not manifest["source_changed_during_run"]
        assert manifest["gold_sha256"] == result["gold_sha256"]
        assert len((folder / "run" / "cases.jsonl").read_text().splitlines()) == 2
