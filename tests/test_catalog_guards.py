"""Catalog guard tests cover ambiguous identities and invalid administrative operations."""

import pytest

from creditlens.corpus import build_demo_pages
from creditlens.domain import Principal
from creditlens.retrieval import EvidenceCatalog, chunk_page, lexical_rank, reciprocal_rank_fusion


def test_duplicate_page_identity_rejected() -> None:
    """Conflicting content under one canonical identity cannot silently replace source evidence."""
    page = build_demo_pages()[0]
    altered = page.model_copy(update={"text": "Contradictory replacement content"})
    with pytest.raises(ValueError, match="Duplicate canonical page identity"):
        EvidenceCatalog((page, altered))


def test_unknown_revocation_preserves_snapshot() -> None:
    """An invalid administrator target fails without invalidating an unrelated current snapshot."""
    page = build_demo_pages()[0]
    catalog = EvidenceCatalog((page,))
    user = Principal(
        subject="underwriter",
        tenant_id=page.tenant_id,
        role="underwriter",
        borrower_ids=("borrower-001",),
        acl_groups=("underwriting",),
        revision=1,
    )
    before, revision = catalog.snapshot(user, "borrower-001", page.valid_from)
    assert before
    with pytest.raises(ValueError, match="Unknown evidence chunk"):
        catalog.revoke("nonexistent")
    catalog.verify_revision(revision)
    assert catalog.snapshot(user, "borrower-001", page.valid_from) == (before, revision)


@pytest.mark.parametrize("limit", [0, 101])
def test_invalid_ranking_budget(limit: int) -> None:
    """Invalid ranking limits fail even for an empty scope rather than changing query semantics."""
    with pytest.raises(ValueError, match="Retrieval limit"):
        lexical_rank("policy", (), limit=limit)
    assert lexical_rank("policy", ()) == ()


@pytest.mark.parametrize("size", [199, 8001])
def test_invalid_chunk_budget(size: int) -> None:
    """Reject budgets outside the reviewed evidence bounds before constructing source spans."""
    with pytest.raises(ValueError, match="Chunk size"):
        chunk_page(build_demo_pages()[0], size=size)


@pytest.mark.parametrize(("limit", "k"), [(0, 60), (10, 0)])
def test_invalid_fusion_parameters(limit: int, k: int) -> None:
    """Undefined rank-fusion parameters must fail rather than produce misleading empty results."""
    with pytest.raises(ValueError, match="RRF limits"):
        reciprocal_rank_fusion((), limit=limit, k=k)
