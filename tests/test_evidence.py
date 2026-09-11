"""Critical evidence tests exercise identity, ranking and financial failure boundaries."""

from datetime import date
from decimal import Decimal

import pytest

from creditlens.citations import cite, quote, validate_citation, validate_extract
from creditlens.corpus import build_demo_pages
from creditlens.domain import Claim, Principal
from creditlens.errors import ServiceError
from creditlens.finance import calculate_review, dscr
from creditlens.retrieval import EvidenceCatalog, chunk_page, lexical_rank, reciprocal_rank_fusion


def principal() -> Principal:
    """An underwriter cannot inspect another tenant, borrower, or credit-officer-only page."""
    return Principal(
        subject="demo",
        tenant_id="demo-bank",
        role="underwriter",
        borrower_ids=tuple(f"borrower-{n:03}" for n in range(1, 6)),
        acl_groups=("underwriting",),
        revision=1,
    )


def test_filter_before_rank_and_revoke() -> None:
    """Forbidden high-score content is absent from candidates, and warm snapshots invalidate."""
    catalog = EvidenceCatalog(build_demo_pages())
    candidates, revision = catalog.snapshot(principal(), "borrower-001", date(2026, 2, 1))
    assert all(c.borrower_id in (None, "borrower-001") for c in candidates)
    assert all(c.acl_groups == ("underwriting",) for c in candidates)
    assert all(c.document_version == "v2" for c in candidates if c.borrower_id is None)
    ranking = lexical_rank("RESTRICTED-001 credit officer watchlist", candidates)
    assert not any("RESTRICTED-001" in c.text for c in ranking)
    catalog.revoke(candidates[0].chunk_id)
    with pytest.raises(ServiceError, match="evidence_changed"):
        catalog.verify_revision(revision)
    next_candidates, _ = catalog.snapshot(principal(), "borrower-001", date(2026, 2, 1))
    assert candidates[0] not in next_candidates


def test_chunk_provenance_and_forged_citations() -> None:
    """Chunks reconstruct exact page text, and valid-looking IDs cannot support invented claims."""
    page = build_demo_pages()[0]
    chunks = chunk_page(page, size=200)
    assert "".join(c.text for c in chunks) == page.text
    assert chunks == chunk_page(page, size=200)
    assert all(c.text == page.text[c.start_char : c.end_char] for c in chunks)
    citation = cite(chunks[0])
    validate_extract(quote(chunks[0]), chunks)
    with pytest.raises(ServiceError, match="invalid_citation"):
        validate_citation(citation.model_copy(update={"page": 999}), chunks)
    with pytest.raises(ServiceError, match="unsupported_claim"):
        validate_extract(Claim(text="This loan is approved", citations=(citation,)), chunks)
    with pytest.raises(ServiceError, match="invalid_citation"):
        validate_citation(citation, ())


@pytest.mark.parametrize(
    ("borrower", "disposition", "ratio"),
    [
        ("borrower-001", "MEETS_POLICY", Decimal("1.5000")),
        ("borrower-002", "EXCEPTION_REQUIRED", Decimal("1.1000")),
        ("borrower-003", "INSUFFICIENT_EVIDENCE", None),
        ("borrower-004", "MATERIAL_CONFLICT", None),
    ],
)
def test_finance_outcomes(borrower: str, disposition: str, ratio: Decimal | None) -> None:
    """Missing and contradictory inputs cannot be converted into a confident policy result."""
    chunks, _ = EvidenceCatalog(build_demo_pages()).snapshot(
        principal(), borrower, date(2026, 2, 1)
    )
    result = calculate_review(chunks)
    assert result.disposition == disposition
    assert (result.metrics[0].value if result.metrics else None) == ratio
    if result.metrics:
        for citation in result.metrics[0].citations:
            validate_citation(citation, chunks)


@pytest.mark.parametrize("debt", [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_invalid_financial_denominators(debt: Decimal) -> None:
    """A non-finite or nonpositive denominator has no valid DSCR interpretation."""
    with pytest.raises(ValueError):
        dscr(Decimal("100"), debt)


def test_rrf_known_order() -> None:
    """Rank fusion deduplicates repeated IDs and favors evidence supported by both rankers."""
    chunks = tuple(chunk_page(page)[0] for page in build_demo_pages()[:3])
    a, b, c = chunks
    assert reciprocal_rank_fusion(((a, b, c), (b, c, a)))[0] == b
    assert len(reciprocal_rank_fusion(((a, a),))) == 1


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("1.25 ratio", "1..25 ratio"),
        ("1.25 ratio", "0 ratio"),
        ("180000.00", "1e1000"),
        ("180000.00", "NaN"),
        ("currency=USD", "currency=unknown"),
        ("period=2025", "period=tomorrow"),
    ],
)
def test_malformed_values_abstain(old: str, new: str) -> None:
    """Malformed thresholds and extreme finite amounts must not escape as crashes or ratios."""
    pages = tuple(
        page.model_copy(update={"text": page.text.replace(old, new)}) for page in build_demo_pages()
    )
    chunks, _ = EvidenceCatalog(pages).snapshot(principal(), "borrower-001", date(2026, 2, 1))
    assert calculate_review(chunks).disposition == "INSUFFICIENT_EVIDENCE"


def test_page_revocation_covers_siblings() -> None:
    """A split page cannot leak remaining fragments after a page-level permission change."""
    page = build_demo_pages()[0].model_copy(update={"text": "policy evidence " * 300})
    catalog = EvidenceCatalog((page,))
    chunks, _ = catalog.snapshot(principal(), "borrower-001", date(2025, 6, 1))
    assert len(chunks) > 1
    catalog.revoke(chunks[0].chunk_id)
    assert catalog.snapshot(principal(), "borrower-001", date(2025, 6, 1))[0] == ()


@pytest.mark.parametrize("replacement", ["no-policy", "unknown threshold", "borrower-policy"])
def test_missing_or_untrusted_threshold(replacement: str) -> None:
    """A borrower page cannot impersonate lender policy, and unknown rules force abstention."""
    chunks, _ = EvidenceCatalog(build_demo_pages()).snapshot(
        principal(), "borrower-001", date(2026, 2, 1)
    )
    edited = []
    for chunk in chunks:
        if chunk.section == "dscr.threshold":
            if replacement == "no-policy":
                continue
            chunk = chunk.model_copy(
                update={"borrower_id": "borrower-001"}
                if replacement == "borrower-policy"
                else {"text": replacement}
            )
        edited.append(chunk)
    assert calculate_review(tuple(edited)).disposition == "INSUFFICIENT_EVIDENCE"


def test_equivalent_numbers_are_not_conflicts() -> None:
    """Formatting variations preserve numerical meaning while malformed amounts abstain."""
    from creditlens.finance import normalized_fact

    assert normalized_fact("operating_cash_flow", "180000.0") == normalized_fact(
        "operating_cash_flow", "180000.00"
    )
    assert normalized_fact("operating_cash_flow", "broken") == "broken"
