"""Deterministic DSCR preparation never substitutes model arithmetic for cited inputs."""

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, DecimalException, localcontext
from typing import Literal

from creditlens.citations import cite, quote
from creditlens.domain import Chunk, Claim, FinancialMetric

Disposition = Literal[
    "MEETS_POLICY",
    "EXCEPTION_REQUIRED",
    "INSUFFICIENT_EVIDENCE",
    "MATERIAL_CONFLICT",
    "HUMAN_JUDGMENT_REQUIRED",
]
FIELDS = ("operating_cash_flow", "annual_debt_service", "currency", "period")


@dataclass(frozen=True)
class FinanceResult:
    """Partial or inconsistent evidence is represented explicitly rather than filled in."""

    disposition: Disposition
    metrics: tuple[FinancialMetric, ...] = ()
    missing: tuple[str, ...] = ()
    conflicts: tuple[Claim, ...] = ()


def source_fields(chunks: tuple[Chunk, ...]) -> dict[str, list[tuple[str, Chunk]]]:
    """Parse only explicit fixture fields; arbitrary financial prose needs reviewed extraction."""
    fields: dict[str, list[tuple[str, Chunk]]] = {key: [] for key in FIELDS}
    for chunk in chunks:
        if chunk.borrower_id is None:
            continue
        for key, value in re.findall(
            r"\b(operating_cash_flow|annual_debt_service|currency|period)=([^\s]+)", chunk.text
        ):
            fields[key].append((value.rstrip(".,;"), chunk))
    return fields


def dscr(cash_flow: Decimal, debt_service: Decimal) -> Decimal:
    """Reject non-finite or invalid denominators and retain precision for policy comparisons."""
    if not cash_flow.is_finite() or not debt_service.is_finite() or debt_service <= 0:
        raise ValueError("DSCR requires finite inputs and positive annual debt service")
    if abs(cash_flow) > Decimal("1e18") or not Decimal("0.01") <= debt_service <= Decimal("1e18"):
        raise ValueError("Financial inputs exceed the supported money range")
    with localcontext() as context:
        context.prec = 40
        return cash_flow / debt_service


def conflicting_sources(fields: dict[str, list[tuple[str, Chunk]]]) -> tuple[Claim, ...]:
    """Keep both sides of a disagreement; averaging would invent an unsupported input."""
    conflicts: dict[str, Claim] = {}
    for key, values in fields.items():
        if len({normalized_fact(key, value) for value, _ in values}) > 1:
            for _, chunk in values:
                conflicts[chunk.chunk_id] = quote(chunk)
    return tuple(conflicts.values())


def normalized_fact(key: str, value: str) -> str:
    """Equivalent decimal spellings must not create a false evidence conflict."""
    if key in ("currency", "period"):
        return value
    try:
        number = Decimal(value)
        return str(number.normalize()) if number.is_finite() else value
    except DecimalException:
        return value


def calculate_review(evidence: tuple[Chunk, ...]) -> FinanceResult:
    """Evaluate only the synthetic DSCR rule, requiring complete, consistent cited fields."""
    fields = source_fields(evidence)
    conflicts = conflicting_sources(fields)
    if conflicts:
        return FinanceResult("MATERIAL_CONFLICT", conflicts=conflicts)
    missing = tuple(key for key, values in fields.items() if not values)
    if missing:
        return FinanceResult("INSUFFICIENT_EVIDENCE", missing=missing)
    policies = [
        chunk
        for chunk in evidence
        if chunk.section == "dscr.threshold"
        and chunk.borrower_id is None
        and chunk.document_kind == "policy"
    ]
    if len(policies) != 1:
        return FinanceResult("INSUFFICIENT_EVIDENCE", missing=("applicable DSCR policy",))
    match = re.search(r"policy threshold is ([0-9.]+) ratio", policies[0].text)
    if match is None:
        return FinanceResult("INSUFFICIENT_EVIDENCE", missing=("verified DSCR threshold",))
    try:
        threshold = Decimal(match[1])
        if not threshold.is_finite() or not Decimal("0") < threshold < Decimal("100"):
            raise ValueError("Invalid threshold")
    except (DecimalException, ValueError):
        return FinanceResult("INSUFFICIENT_EVIDENCE", missing=("verified DSCR threshold",))
    return evaluate_ratio(fields, threshold, policies[0])


def evaluate_ratio(
    fields: dict[str, list[tuple[str, Chunk]]], threshold: Decimal, policy: Chunk
) -> FinanceResult:
    """Compare the unrounded ratio, then round only its display value to four decimal places."""
    try:
        if not re.fullmatch(r"[A-Z]{3}", fields["currency"][0][0]) or not re.fullmatch(
            r"20[0-9]{2}", fields["period"][0][0]
        ):
            raise ValueError("Unsupported currency or annual period")
        ratio = dscr(
            Decimal(fields["operating_cash_flow"][0][0]),
            Decimal(fields["annual_debt_service"][0][0]),
        )
        display_value = ratio.quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN)
    except (DecimalException, ValueError):
        return FinanceResult("INSUFFICIENT_EVIDENCE", missing=("valid financial inputs",))
    citations = {chunk.chunk_id: cite(chunk) for values in fields.values() for _, chunk in values}
    citations[policy.chunk_id] = cite(policy)
    metric = FinancialMetric(
        name="DSCR",
        value=display_value,
        unit="ratio",
        source_fields=FIELDS,
        citations=tuple(citations.values()),
    )
    return FinanceResult("MEETS_POLICY" if ratio >= threshold else "EXCEPTION_REQUIRED", (metric,))
