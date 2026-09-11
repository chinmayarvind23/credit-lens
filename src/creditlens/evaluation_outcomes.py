"""Execution and deterministic fixture outcomes do not claim independent semantic judgment."""

import json
import re
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

from sqlalchemy import func, select

import creditlens.workflow as workflow_module
from creditlens.domain import Packet, QueryRequest
from creditlens.errors import ServiceError
from creditlens.evaluation import GoldCase, audit_chunks, page_key
from creditlens.retrieval import EvidenceCatalog, lexical_rank
from creditlens.storage import GrantStore, audit_events, grants, open_database
from creditlens.workflow import QueryWorkflow, validate_packet


def normalized(text: str) -> str:
    """Normalize numeric thousands separators and simple token boundaries for debugging anchors."""
    return re.sub(r"[^a-z0-9_.]+", " ", text.lower().replace(",", "")).strip()


def behavior_pass(packet: Packet, expected: str) -> bool:
    """Check explicit dispositions while leaving semantic refusal quality unmeasured."""
    dispositions = {
        "insufficient_evidence": "INSUFFICIENT_EVIDENCE",
        "exception_required": "EXCEPTION_REQUIRED",
        "material_conflict": "MATERIAL_CONFLICT",
    }
    if expected in dispositions:
        return packet.policy_disposition == dispositions[expected]
    if expected == "deny":
        return False
    if expected == "deny_or_abstain":
        return packet.abstained
    return bool(packet.borrower_summary or packet.applicable_policy or packet.abstained)


def metric_pass(packet: Packet, case: GoldCase) -> bool | None:
    """Compare calculator output with authored Decimal gold and its explicit tolerance."""
    if case.expected_metric is None:
        return None
    expected = case.expected_metric
    return any(
        metric.name.lower() == expected.name.lower()
        and abs(metric.value - Decimal(expected.value)) <= Decimal(expected.absolute_tolerance)
        for metric in packet.calculated_metrics
    )


def expected_path(stages: Sequence[str], retrieval_stage: str = "retrieval.local_bm25") -> bool:
    """Demand the mandatory ordered stages while allowing documented query branches."""
    if retrieval_stage not in {"retrieval.local_bm25", "retrieval.provider"}:
        raise ValueError("Unknown evaluated retrieval path")
    required = (
        "auth.resolve_current_grant",
        "authorization.filter_before_retrieval",
        retrieval_stage,
        "citation.validate_exact_extracts",
        "authorization.recheck",
        "audit.persist",
    )
    positions = [stages.index(stage) if stage in stages else -1 for stage in required]
    return all(position >= 0 for position in positions) and positions == sorted(set(positions))


def packet_checks(
    packet: Packet,
    case: GoldCase,
    store: GrantStore,
    *,
    retrieval_stage: str = "retrieval.local_bm25",
) -> dict[str, Any]:
    """Inspect persisted audit state and context alongside deterministic fixture checks."""
    validate_packet(packet)
    text = normalized(
        " ".join(
            claim.text
            for claim in packet.borrower_summary
            + packet.applicable_policy
            + packet.exceptions
            + packet.contradictions
        )
    )
    anchors = {term: normalized(term) in text for term in case.expected_terms}
    behavior = behavior_pass(packet, case.expected_behavior)
    numeric = metric_pass(packet, case)
    violations = audit_chunks(packet.evidence, case)
    stages = [stage.name for stage in packet.stages]
    with store.engine.connect() as connection:
        audit = (
            connection.execute(
                select(audit_events).where(audit_events.c.request_id == packet.request_id)
            )
            .mappings()
            .first()
        )
    path_pass = expected_path(stages, retrieval_stage) and audit is not None
    if audit is not None:
        path_pass = path_pass and audit["event"]["chunk_ids"] == [
            c.chunk_id for c in packet.evidence
        ]
    return {
        "error": None,
        "fixture_pass": behavior
        and all(anchors.values())
        and numeric is not False
        and not violations
        and path_pass,
        "behavior_pass": behavior,
        "anchor_checks": anchors,
        "metric_pass": numeric,
        "structural_support_pass": True,
        "path_pass": path_pass,
        "audit_persisted": audit is not None,
        "context_violations": violations,
        "context_chunk_ids": [c.chunk_id for c in packet.evidence],
        "context_pages": [page_key(c) for c in packet.evidence],
        "stages": stages,
        "workflow_latency_ms": packet.latency_ms,
        "disposition": packet.policy_disposition,
        "semantic_rubric_grade": None,
        "semantic_refusal_grade": None,
        "packet": packet.model_dump(mode="json"),
    }


def failed_outcome(error: Exception, case: GoldCase, observed: dict[str, int]) -> dict[str, Any]:
    """Expected access denial is a correct path; every other scheduled failure remains a failure."""
    denied = (
        isinstance(error, ServiceError) and error.status == 403 and case.expected_behavior == "deny"
    )
    safe_path = denied and all(
        observed.get(key) == 0 for key in ("ranking_calls", "context_calls", "audit_writes")
    )
    return {
        "error": None
        if denied
        else (error.code if isinstance(error, ServiceError) else type(error).__name__),
        "fixture_pass": safe_path,
        "path_pass": safe_path,
        "context_violations": [],
        "denied": denied,
        "denial_path_observation": observed,
        "semantic_rubric_grade": None,
    }


def audit_count(store: GrantStore) -> int:
    """Observe persisted writes around denial instead of inferring effects from HTTP status."""
    with store.engine.connect() as connection:
        return int(connection.execute(select(func.count()).select_from(audit_events)).scalar_one())


def execute_case(workflow: QueryWorkflow, case: GoldCase, store: GrantStore) -> dict[str, Any]:
    """Serial evaluator spies preserve behavior while observing denied query work and state."""
    before = audit_count(store)
    with (
        patch("creditlens.workflow.lexical_rank", wraps=lexical_rank) as ranking,
        patch(
            "creditlens.workflow.collect_context", wraps=workflow_module.collect_context
        ) as context,
    ):
        try:
            packet = workflow.query(
                QueryRequest(
                    borrower_id=case.borrower_id,
                    question=case.question,
                    effective_at=case.effective_at,
                ),
                case.principal(),
            )
            return packet_checks(packet, case, store)
        except Exception as failure:
            observed = {
                "ranking_calls": ranking.call_count,
                "context_calls": context.call_count,
                "audit_writes": audit_count(store) - before,
            }
            return failed_outcome(failure, case, observed)


def add_outcomes(
    records: list[dict[str, Any]], cases: Sequence[GoldCase], catalog: EvidenceCatalog, output: Path
) -> None:
    """Seed per-case trusted SQL grants and preserve real audit events in the run directory."""
    store = GrantStore(
        open_database(f"sqlite:///{(output / 'audit.sqlite3').resolve().as_posix()}")
    )
    workflow = QueryWorkflow(catalog, store)
    try:
        for record, case in zip(records, cases, strict=True):
            principal = case.principal()
            with store.engine.begin() as connection:
                connection.execute(grants.delete().where(grants.c.subject == principal.subject))
                connection.execute(grants.insert().values(**principal.model_dump(), enabled=True))
            record["outcome"] = execute_case(workflow, case, store)
        with store.engine.connect() as connection:
            audits = [dict(row) for row in connection.execute(select(audit_events)).mappings()]
        (output / "audit-events.json").write_text(
            json.dumps(audits, indent=2) + "\n", encoding="utf-8"
        )
    finally:
        store.engine.dispose()
