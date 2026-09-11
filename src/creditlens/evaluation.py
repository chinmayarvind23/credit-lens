"""Reproducible authored-page retrieval metrics and independent authorization auditing."""

import json
import math
import os
import platform
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from statistics import mean
from time import perf_counter
from typing import Any, Literal

from pydantic import Field, model_validator

from creditlens.domain import Chunk, Page, Principal, StrictModel
from creditlens.errors import ServiceError
from creditlens.retrieval import EvidenceCatalog, lexical_rank

PageKey = tuple[str, str, int]
MODE = "local-bm25-control"
METRIC_CONTRACT = "authored-unique-page-exponential-gain-v1"


class Relevance(StrictModel):
    """Physical page labels survive changes to chunk boundaries and ranking implementations."""

    document_id: str
    document_version: str
    page: int = Field(ge=1)
    relevance: int = Field(default=2, ge=0, le=3)


class ExpectedMetric(StrictModel):
    """An explicit numeric tolerance is part of gold, not inferred from measured performance."""

    name: str
    value: str
    absolute_tolerance: str


class GoldCase(StrictModel):
    """Reject unknown fixture fields so a changed gold contract requires evaluator review."""

    case_id: str
    gold_version: str
    category: str
    question: str
    borrower_id: str
    tenant_id: str
    principal_subject: str
    principal_role: Literal["underwriter", "admin", "reviewer"]
    borrower_grants: tuple[str, ...]
    acl_groups: tuple[str, ...]
    effective_at: date
    relevant_pages: tuple[Relevance, ...]
    expected_behavior: str
    expected_terms: tuple[str, ...]
    forbidden_terms: tuple[str, ...]
    retrieval_eligible: bool
    answer_rubric: str
    expected_metric: ExpectedMetric | None = None
    forbidden_pages: tuple[Relevance, ...] = ()
    forbidden_tenant: str | None = None

    @model_validator(mode="after")
    def valid_qrels(self) -> "GoldCase":
        """Duplicated labels and inconsistent eligibility cannot inflate metric denominators."""
        keys = [page_key(qrel) for qrel in self.relevant_pages]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate gold page identity")
        if self.retrieval_eligible != any(qrel.relevance > 0 for qrel in self.relevant_pages):
            raise ValueError("Retrieval eligibility must match positive authorized qrels")
        return self

    def principal(self) -> Principal:
        """Use declared evaluation grants instead of inheriting the browser demo's limited scope."""
        return Principal(
            subject=self.principal_subject,
            role=self.principal_role,
            tenant_id=self.tenant_id,
            borrower_ids=self.borrower_grants,
            acl_groups=self.acl_groups,
            revision=1,
        )


@dataclass(frozen=True)
class RankingScore:
    """Undefined metrics remain null for no-positive-qrel cases, never fabricated perfect scores."""

    eligible: bool
    retrieved_relevant: int
    relevant_total: int
    recall: float | None
    ndcg: float | None
    reciprocal_rank: float | None


def page_key(page: Page | Relevance) -> PageKey:
    """Scope is audited separately; the gold scoring unit is document/version/physical page."""
    return page.document_id, page.document_version, page.page


def unique_pages(ranking: Sequence[PageKey]) -> tuple[PageKey, ...]:
    """Keep first occurrence order so multiple chunks from one page cannot earn repeated credit."""
    return tuple(dict.fromkeys(ranking))


def dcg(grades: Sequence[int]) -> float:
    """Use the frozen exponential-gain definition, including rank penalties for nonrelevant hits."""
    if any(grade < 0 or grade > 3 for grade in grades):
        raise ValueError("Relevance grades must be between zero and three")
    return float(
        sum((2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(grades, start=1))
    )


def score_ranking(
    ranking: Sequence[PageKey], qrels: Mapping[PageKey, int], k: int = 10
) -> RankingScore:
    """Score unique pages with the full positive-qrel denominator and truncated ideal ranking."""
    if k < 1:
        raise ValueError("Metric cutoff must be positive")
    dcg(tuple(qrels.values()))
    positive = {key: grade for key, grade in qrels.items() if grade > 0}
    if not positive:
        return RankingScore(False, 0, 0, None, None, None)
    top = unique_pages(ranking)[:k]
    grades = [positive.get(key, 0) for key in top]
    hits = sum(grade > 0 for grade in grades)
    ideal = dcg(sorted(positive.values(), reverse=True)[:k])
    reciprocal = next((1 / rank for rank, grade in enumerate(grades, 1) if grade), 0.0)
    return RankingScore(
        True, hits, len(positive), hits / len(positive), dcg(grades) / ideal, reciprocal
    )


def scope_violations(page: Page, case: GoldCase) -> tuple[str, ...]:
    """Audit declarative gold scope independently from production authorization code."""
    checks = {
        "borrower_grant": case.borrower_id in case.borrower_grants,
        "tenant": page.tenant_id == case.tenant_id,
        "borrower": page.borrower_id in (None, case.borrower_id),
        "acl": bool(set(page.acl_groups) & set(case.acl_groups)),
        "effective_from": page.valid_from <= case.effective_at,
        "effective_to": page.valid_to is None or case.effective_at < page.valid_to,
        "extraction_quality": page.extraction_confidence >= 0.9,
        "forbidden_page": page_key(page) not in {page_key(p) for p in case.forbidden_pages},
        "forbidden_tenant": case.forbidden_tenant is None
        or page.tenant_id != case.forbidden_tenant,
    }
    return tuple(name for name, passed in checks.items() if not passed)


def audit_chunks(chunks: Sequence[Chunk], case: GoldCase) -> list[dict[str, Any]]:
    """Retain violating IDs and reasons so a zero-violation aggregate has inspectable evidence."""
    return [
        {"chunk_id": chunk.chunk_id, "page": page_key(chunk), "reasons": failures}
        for chunk in chunks
        if (failures := scope_violations(chunk, case))
    ]


def read_inputs(gold_path: Path, pages_path: Path) -> tuple[tuple[GoldCase, ...], tuple[Page, ...]]:
    """Reject corrupt extracted pages and unauthorized qrels before scoring."""
    cases = tuple(
        GoldCase.model_validate_json(line)
        for line in gold_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    pages = tuple(
        Page.model_validate_json(line)
        for line in pages_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if not cases or len({case.case_id for case in cases}) != len(cases):
        raise ValueError("Gold requires nonempty unique case IDs")
    for page in pages:
        if sha256(page.text.encode()).hexdigest() != page.content_hash:
            raise ValueError("Canonical extracted page hash mismatch")
    lookup = {(page.tenant_id, *page_key(page)): page for page in pages}
    for case in cases:
        for qrel in case.relevant_pages:
            labeled_page = lookup.get((case.tenant_id, *page_key(qrel)))
            if labeled_page is None or scope_violations(labeled_page, case):
                raise ValueError(f"Gold qrel is missing or unauthorized: {case.case_id}")
    return cases, pages


def rank_case(catalog: EvidenceCatalog, case: GoldCase) -> dict[str, Any]:
    """Measure lexical ranking only; metadata lookups in a packet never boost this ranking score."""
    started = perf_counter()
    candidates: tuple[Chunk, ...] = ()
    ranking: tuple[Chunk, ...] = ()
    error = None
    denied = False
    try:
        candidates, revision = catalog.snapshot(
            case.principal(), case.borrower_id, case.effective_at
        )
        ranking = lexical_rank(case.question, candidates, limit=100)
        catalog.verify_revision(revision)
    except ServiceError as failure:
        denied = failure.status == 403 and case.expected_behavior == "deny"
        error = None if denied else failure.code
    except Exception as failure:
        error = type(failure).__name__
    if error is not None:
        ranking = ()
    score = score_ranking(
        [page_key(chunk) for chunk in ranking],
        {page_key(qrel): qrel.relevance for qrel in case.relevant_pages},
    )
    return {
        "case_id": case.case_id,
        "category": case.category,
        "principal": case.principal().model_dump(mode="json"),
        "effective_at": case.effective_at.isoformat(),
        "expected_behavior": case.expected_behavior,
        "score": asdict(score),
        "candidate_count": len(candidates),
        "candidate_chunk_ids": [chunk.chunk_id for chunk in candidates],
        "ranked_chunk_ids": [chunk.chunk_id for chunk in ranking],
        "ranked_pages": unique_pages([page_key(chunk) for chunk in ranking]),
        "candidate_violations": audit_chunks(candidates, case),
        "ranking_violations": audit_chunks(ranking, case),
        "denied_before_ranking": denied,
        "error": error,
        "ranking_latency_ms": (perf_counter() - started) * 1000,
    }


def percentile(values: Sequence[float], quantile: float) -> float | None:
    """Use nearest-rank percentiles and record their definition for reproducible latency reports."""
    if not 0 < quantile <= 1:
        raise ValueError("Quantile must be in (0, 1]")
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(quantile * len(values)) - 1)]


def summarize_ranking(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Keep macro, micro and excluded counts separate from security results."""
    eligible = [record["score"] for record in records if record["score"]["eligible"]]
    relevant = sum(score["relevant_total"] for score in eligible)
    return {
        "scheduled_cases": len(records),
        "eligible_cases": len(eligible),
        "excluded_no_qrel_cases": len(records) - len(eligible),
        "retrieved_relevant_pages": sum(score["retrieved_relevant"] for score in eligible),
        "gold_relevant_pages": relevant,
        "macro_recall_at_10": mean(score["recall"] for score in eligible) if eligible else None,
        "micro_recall_at_10": sum(score["retrieved_relevant"] for score in eligible) / relevant
        if relevant
        else None,
        "ndcg_at_10": mean(score["ndcg"] for score in eligible) if eligible else None,
        "mrr_at_10": mean(score["reciprocal_rank"] for score in eligible) if eligible else None,
        "p95_ranking_latency_ms": percentile([r["ranking_latency_ms"] for r in records], 0.95),
        "mean_ranking_latency_ms": mean(r["ranking_latency_ms"] for r in records)
        if records
        else None,
    }


def source_hashes(repo: Path) -> dict[str, str]:
    """Capture exact imported implementation bytes even when another agent owns uncommitted work."""
    paths = sorted((repo / "src" / "creditlens").glob("*.py"))
    paths += [repo / "scripts" / "evaluate.py", repo / "uv.lock"]
    return {
        str(path.relative_to(repo)).replace("\\", "/"): sha256(path.read_bytes()).hexdigest()
        for path in paths
        if path.exists()
    }


def git_metadata(repo: Path) -> dict[str, Any]:
    """Fixed read-only Git commands record source state without reading remote credentials."""
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("Git is required for a reproducible source manifest")
    revision = subprocess.run(  # noqa: S603 - resolved executable and fixed read-only arguments
        [executable, "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    status = subprocess.run(  # noqa: S603 - resolved executable and fixed read-only arguments
        [executable, "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return {"revision": revision, "dirty": bool(status), "status": status}


def run_manifest(repo: Path, gold_path: Path, pages_path: Path) -> dict[str, Any]:
    """Record scope, execution conditions and absent providers alongside every measured result."""
    return {
        "started_at_utc": datetime.now(UTC).isoformat(),
        "git": git_metadata(repo),
        "source_hashes": source_hashes(repo),
        "gold_sha256": sha256(gold_path.read_bytes()).hexdigest(),
        "pages_sha256": sha256(pages_path.read_bytes()).hexdigest(),
        "mode": MODE,
        "metric_contract": METRIC_CONTRACT,
        "split": "frozen-synthetic-full",
        "seed": 0,
        "concurrency": 1,
        "cache": "disabled",
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "logical_cpu_count": os.cpu_count(),
        "dependencies": {name: version(name) for name in ("pypdf", "pydantic", "sqlalchemy")},
        "ranking": {
            "chunker": "paragraph-v1-1200",
            "chunk_limit": 100,
            "unique_page_cutoff": 10,
            "k1": 1.2,
            "b": 0.75,
            "tie_break": "ascending chunk_id",
            "latency_percentile": "nearest-rank",
            "latency_scope": "authorization, ranking, metric computation "
            "and independent scope audit; "
            "excludes catalog loading and workflow execution",
        },
        "embedding_model": None,
        "reranker": None,
        "generator_model": None,
        "judge_model": None,
        "prompt_version": None,
        "pricing_provenance": None,
    }


def evaluate(
    gold_path: Path, pages_path: Path, output: Path, repo: Path, outcomes: bool = False
) -> dict[str, Any]:
    """Preserve every scheduled record before reporting metrics or executing a release gate."""
    output.mkdir(parents=True, exist_ok=False)
    manifest = run_manifest(repo, gold_path, pages_path)
    cases, pages = read_inputs(gold_path, pages_path)
    catalog = EvidenceCatalog(pages)
    records = [rank_case(catalog, case) for case in cases]
    if outcomes:
        from creditlens.evaluation_outcomes import add_outcomes

        add_outcomes(records, cases, catalog, output)
    summary = summarize_run(records, manifest, pages, catalog)
    manifest["finished_at_utc"] = datetime.now(UTC).isoformat()
    manifest["source_changed_during_run"] = manifest["source_hashes"] != source_hashes(repo)
    for name, value in (("manifest.json", manifest), ("summary.json", summary)):
        (output / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    (output / "cases.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return summary


def summarize_run(
    records: list[dict[str, Any]],
    manifest: dict[str, Any],
    pages: tuple[Page, ...],
    catalog: EvidenceCatalog,
) -> dict[str, Any]:
    """Separate ranking, execution and unmeasured semantic targets in the public result schema."""
    return {
        "mode": MODE,
        "metric_contract": METRIC_CONTRACT,
        "gold_sha256": manifest["gold_sha256"],
        "pages_sha256": manifest["pages_sha256"],
        "catalog_version": catalog.version,
        "physical_pages": len(pages),
        "ranking": summarize_ranking(records),
        "slices": {
            category: summarize_ranking([r for r in records if r["category"] == category])
            for category in sorted({r["category"] for r in records})
        },
        "execution": {
            "unauthorized_candidates": sum(len(r["candidate_violations"]) for r in records),
            "unauthorized_ranked": sum(len(r["ranking_violations"]) for r in records),
            "ranking_errors": sum(r["error"] is not None for r in records),
            "denied_cases": sum(r["denied_before_ranking"] for r in records),
        },
        "outcomes": summarize_outcomes(records),
        "targets": {
            "recall_at_10": {"target": 0.941, "status": "local authored-qrel control only"},
            "ndcg_at_10": {"target": 0.89, "status": "local authored-qrel control only"},
            "citation_precision": {"target": 0.968, "measured": None},
            "grounded_answer_pass_rate": {"target": 0.925, "measured": None},
            "unsupported_claim_rate": {"target": 0.017, "measured": None},
            "provider_p95_seconds": {"target": 2.7, "measured": None},
            "average_llm_cost_usd": {"target": 0.043, "measured": None},
        },
        "limitations": [
            "Authored page qrels are not exhaustive blinded labels.",
            "Sparse synthetic corpus has 98 templates and repeated adverse scenarios.",
            "No independent semantic judge, Cortex baseline or cloud latency is measured.",
        ],
    }


def summarize_outcomes(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Structural and fixture checks stay explicitly distinct from independent semantic quality."""
    outcomes = [record["outcome"] for record in records if "outcome" in record]
    if not outcomes:
        return {"status": "not_run", "unauthorized_context": None}
    return {
        "status": "deterministic_fixture_only",
        "scheduled": len(outcomes),
        "fixture_passes": sum(item["fixture_pass"] for item in outcomes),
        "fixture_pass_rate": mean(item["fixture_pass"] for item in outcomes),
        "unauthorized_context": sum(len(item["context_violations"]) for item in outcomes),
        "path_failures": sum(not item["path_pass"] for item in outcomes),
        "errors": dict(Counter(item["error"] for item in outcomes if item["error"])),
        "semantic_groundedness": None,
        "semantic_citation_precision": None,
    }


def gate_results(
    current: dict[str, Any], baseline: dict[str, Any] | None, policy: dict[str, Any]
) -> list[str]:
    """Security is an unconditional veto; quality compares only compatible recorded runs."""
    failures = [
        key for key in policy["zero_tolerance_execution"] if current["execution"].get(key) != 0
    ]
    if current["ranking"]["scheduled_cases"] != policy["scheduled_cases"]:
        failures.append("scheduled_case_count")
    if current["ranking"]["eligible_cases"] != policy["eligible_cases"]:
        failures.append("eligible_case_count")
    failures.extend(outcome_gate_failures(current))
    if baseline is None:
        return failures
    if current["outcomes"].get("status") != baseline["outcomes"].get("status"):
        failures.append("incompatible_outcome_scope")
    for key in ("gold_sha256", "pages_sha256", "metric_contract"):
        if current[key] != baseline[key]:
            failures.append(f"incompatible_{key}")
    if failures:
        return failures
    return failures + regression_failures(current["ranking"], baseline["ranking"], policy)


def outcome_gate_failures(current: dict[str, Any]) -> list[str]:
    """Executed workflow checks must contain complete observations; omitted fields fail closed."""
    outcome = current["outcomes"]
    if outcome.get("status") == "not_run":
        return []
    if outcome.get("status") != "deterministic_fixture_only":
        return ["unknown_outcome_scope"]
    required = {
        "unauthorized_context": 0,
        "path_failures": 0,
        "scheduled": current["ranking"]["scheduled_cases"],
        "errors": {},
    }
    return [f"workflow_{key}" for key, expected in required.items() if outcome.get(key) != expected]


def regression_failures(
    current: dict[str, Any], baseline: dict[str, Any], policy: dict[str, Any]
) -> list[str]:
    """Reject relative declines at the configured bound without inventing absolute score floors."""
    failures = []
    for metric in policy["quality_metrics"]:
        previous, measured = baseline[metric], current[metric]
        if previous is None or measured is None:
            failures.append(f"unmeasured_{metric}")
        elif not math.isfinite(previous) or not math.isfinite(measured):
            failures.append(f"nonfinite_{metric}")
        elif not 0 <= previous <= 1 or not 0 <= measured <= 1:
            failures.append(f"invalid_{metric}")
        elif previous > 0 and (
            (Decimal(str(previous)) - Decimal(str(measured))) / Decimal(str(previous))
        ) >= Decimal(str(policy["max_relative_regression"])):
            failures.append(f"regression_{metric}")
    return failures
