"""Deterministic provider doubles test rank fusion without claiming a live dense branch."""

from dataclasses import replace
from datetime import date

import pytest

from creditlens.corpus import build_demo_pages
from creditlens.domain import Chunk, Citation, Principal, QueryRequest
from creditlens.errors import ServiceError
from creditlens.hybrid_provider import HybridProvider, HybridResult
from creditlens.retrieval import EvidenceCatalog, chunk_page
from creditlens.search_provider import SearchResult

PRINCIPAL = Principal(
    subject="fixture",
    tenant_id="demo-bank",
    role="underwriter",
    borrower_ids=("borrower-001",),
    acl_groups=("underwriting",),
    revision=1,
)
REQUEST = QueryRequest(
    borrower_id="borrower-001", question="DSCR policy", effective_at=date(2026, 9, 1)
)
CHUNKS = tuple(chunk_page(page)[0] for page in build_demo_pages() if page.document_version == "v2")[
    :3
]
CATALOG = EvidenceCatalog(build_demo_pages())


class ProviderDouble:
    """A named deterministic double exposes branch ordering, failure and freshness behavior."""

    def __init__(self, chunks: tuple[Chunk, ...], mode: str) -> None:
        """Prepare one immutable branch result and counters for contract assertions."""
        self.result = SearchResult(chunks, PRINCIPAL, REQUEST, 1, mode)
        self.calls = 0
        self.revoked = False
        self.fail_search = False
        self.catalog = CATALOG

    def search(self, request: QueryRequest, principal: Principal, limit: int = 10) -> SearchResult:
        """Return the authored ranking or simulate failure without substituting another provider."""
        self.calls += 1
        if self.fail_search:
            raise ServiceError("search_unavailable", "Search is unavailable")
        return self.result

    def verify(self, result: SearchResult) -> None:
        """Simulate an observed revocation in either required provider."""
        if self.revoked:
            raise ServiceError("evidence_changed", "Evidence changed", 409)

    def citation(self, result: SearchResult, citation: Citation) -> Chunk:
        """The double preserves exact citation checks instead of returning arbitrary chunks."""
        self.verify(result)
        for chunk in result.chunks:
            if (chunk.chunk_id, chunk.document_id, chunk.document_version, chunk.page) == (
                citation.chunk_id,
                citation.document_id,
                citation.document_version,
                citation.page,
            ):
                return chunk
        raise ServiceError("invalid_citation", "Citation is unavailable", 422)


def test_rrf_requires_both_rankings_and_preserves_citations() -> None:
    """A shared second-ranked chunk wins RRF through combined support from both branches."""
    first, shared, third = CHUNKS
    lexical = ProviderDouble((first, shared), "fixture-lexical")
    dense = ProviderDouble((third, shared), "fixture-dense")
    provider = HybridProvider(lexical, dense)
    result = provider.search(REQUEST, PRINCIPAL, 1)
    assert result.chunks == (shared,)
    assert lexical.calls == dense.calls == 1
    citation = Citation(
        chunk_id=shared.chunk_id,
        document_id=shared.document_id,
        document_version=shared.document_version,
        page=shared.page,
    )
    assert provider.citation(result, citation) == shared
    with pytest.raises(ServiceError, match="invalid_citation"):
        provider.citation(result, citation.model_copy(update={"page": 999}))
    with pytest.raises(ServiceError, match="invalid_citation"):
        provider.citation(result, citation.model_copy(update={"chunk_id": first.chunk_id}))
    dense.revoked = True
    with pytest.raises(ServiceError, match="evidence_changed"):
        provider.citation(result, citation)


@pytest.mark.parametrize("change", ["principal", "request", "revision", "metadata"])
def test_mismatched_branch_snapshots_fail_closed(change: str) -> None:
    """Fusion rejects apparently useful hits when branch scope or canonical records disagree."""
    lexical = ProviderDouble(CHUNKS, "fixture-lexical")
    dense = ProviderDouble(CHUNKS, "fixture-dense")
    updates = {
        "principal": {"principal": PRINCIPAL.model_copy(update={"revision": 2})},
        "request": {"request": REQUEST.model_copy(update={"borrower_id": "borrower-002"})},
        "revision": {"catalog_revision": 2},
        "metadata": {"chunks": (CHUNKS[0].model_copy(update={"text": "forged"}),)},
    }
    dense.result = replace(dense.result, **updates[change])
    with pytest.raises(ServiceError):
        HybridProvider(lexical, dense).search(REQUEST, PRINCIPAL)


def test_branch_failure_is_not_a_fallback() -> None:
    """A dense failure prevents publication of lexical-only results under a hybrid label."""
    lexical = ProviderDouble(CHUNKS, "fixture-lexical")
    dense = ProviderDouble(CHUNKS, "fixture-dense")
    dense.fail_search = True
    with pytest.raises(ServiceError, match="search_unavailable"):
        HybridProvider(lexical, dense).search(REQUEST, PRINCIPAL)
    assert lexical.calls == dense.calls == 1


def test_contract_type_and_parameter_bounds() -> None:
    """Reject detached snapshots and invalid budgets before requesting branch work."""
    branch = ProviderDouble(CHUNKS, "fixture")
    with pytest.raises(ValueError):
        HybridProvider(branch, branch, candidates=0)
    provider = HybridProvider(branch, branch, candidates=1)
    with pytest.raises(ValueError):
        provider.search(REQUEST, PRINCIPAL, 2)
    with pytest.raises(ServiceError, match="invalid_search_result"):
        provider.verify(branch.result)
    assert branch.calls == 0


def test_empty_rankings_are_valid_executed_results() -> None:
    """An empty result from both required providers is evidence absence, not skipped execution."""
    lexical = ProviderDouble((), "fixture-lexical")
    dense = ProviderDouble((), "fixture-dense")
    result = HybridProvider(lexical, dense).search(REQUEST, PRINCIPAL)
    assert isinstance(result, HybridResult)
    assert result.chunks == ()
    assert lexical.calls == dense.calls == 1


def test_dense_only_citation_uses_dense_branch_and_forged_output_fails() -> None:
    """Fusion can cite a dense-only hit, while invented fused output has no source branch."""
    lexical = ProviderDouble((), "fixture-lexical")
    dense = ProviderDouble((CHUNKS[0],), "fixture-dense")
    provider = HybridProvider(lexical, dense)
    result = provider.search(REQUEST, PRINCIPAL)
    chunk = CHUNKS[0]
    citation = Citation(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        document_version=chunk.document_version,
        page=chunk.page,
    )
    assert provider.citation(result, citation) == chunk
    other = CHUNKS[1]
    forged = replace(result, chunks=(other,))
    invented = Citation(
        chunk_id=other.chunk_id,
        document_id=other.document_id,
        document_version=other.document_version,
        page=other.page,
    )
    with pytest.raises(ServiceError, match="invalid_citation"):
        provider.citation(forged, invented)


def test_distinct_catalogs_with_equal_epochs_cannot_fuse() -> None:
    """Equal revision integers do not establish shared canonical authority across catalogs."""
    lexical = ProviderDouble(CHUNKS, "fixture-lexical")
    dense = ProviderDouble(CHUNKS, "fixture-dense")
    dense.catalog = EvidenceCatalog(build_demo_pages())
    with pytest.raises(ValueError, match="same authoritative catalog"):
        HybridProvider(lexical, dense)
    dense.catalog = lexical.catalog
    provider = HybridProvider(lexical, dense)
    result = provider.search(REQUEST, PRINCIPAL)
    dense.catalog = EvidenceCatalog(build_demo_pages())
    with pytest.raises(ServiceError, match="search_scope_changed"):
        provider.verify(result)
