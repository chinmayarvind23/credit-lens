"""Required lexical and dense branches fuse ranks only under one authorization snapshot."""

from dataclasses import dataclass
from typing import Protocol

from creditlens.domain import Chunk, Citation, Principal, QueryRequest
from creditlens.errors import ServiceError
from creditlens.retrieval import CanonicalCatalog, reciprocal_rank_fusion
from creditlens.search_provider import SearchProvider, SearchResult


@dataclass(frozen=True)
class HybridResult(SearchResult):
    """Retain both branch snapshots so downstream verification cannot lose a revocation check."""

    branches: tuple[SearchResult, SearchResult]


class CanonicalSearchProvider(SearchProvider, Protocol):
    """This local composition requires one shared authority, not coincident integer epochs."""

    catalog: CanonicalCatalog


class HybridProvider:
    """Compose configured providers without pretending an absent dense service has executed."""

    def __init__(
        self,
        lexical: CanonicalSearchProvider,
        dense: CanonicalSearchProvider,
        *,
        candidates: int = 100,
        k: int = 60,
    ) -> None:
        """Bound both branch workloads and retain the documented RRF k=60 baseline by default."""
        if not 1 <= candidates <= 100 or k <= 0:
            raise ValueError("Invalid hybrid candidate count or RRF constant")
        if lexical.catalog is not dense.catalog:
            raise ValueError("Hybrid providers must share the same authoritative catalog")
        self.providers = (lexical, dense)
        self.catalog = lexical.catalog
        self.candidates = candidates
        self.k = k

    def search(self, request: QueryRequest, principal: Principal, limit: int = 10) -> HybridResult:
        """Every configured branch must succeed; silent fallback would change measured behavior."""
        if not 1 <= limit <= self.candidates:
            raise ValueError("Hybrid result limit exceeds the candidate budget")
        self.verify_authority()
        lexical = self.providers[0].search(request, principal, self.candidates)
        self.providers[0].verify(lexical)
        dense = self.providers[1].search(request, principal, self.candidates)
        branches = (lexical, dense)
        validate_branches(branches, request, principal)
        chunks = reciprocal_rank_fusion(tuple(b.chunks for b in branches), limit=limit, k=self.k)
        mode = f"hybrid-rrf:{lexical.provider_mode}+{dense.provider_mode}"
        result = HybridResult(chunks, principal, request, lexical.catalog_revision, mode, branches)
        self.verify(result)
        return result

    def verify(self, result: SearchResult) -> None:
        """Both provider snapshots must remain valid through the final consumer boundary."""
        if not isinstance(result, HybridResult):
            raise ServiceError("invalid_search_result", "Search result is unavailable", 422)
        self.verify_authority()
        validate_branches(result.branches, result.request, result.principal)
        for provider, branch in zip(self.providers, result.branches, strict=True):
            provider.verify(branch)

    def verify_authority(self) -> None:
        """Reject provider reconfiguration that swaps the common catalog during downstream work."""
        if any(provider.catalog is not self.catalog for provider in self.providers):
            raise ServiceError(
                "search_scope_changed", "Search scope changed; retry the request", 409
            )

    def citation(self, result: SearchResult, citation: Citation) -> Chunk:
        """Citations must appear in the fused output and pass their original provider's checks."""
        self.verify(result)
        if not isinstance(result, HybridResult):
            raise TypeError("Expected a hybrid result")
        if not any(chunk.chunk_id == citation.chunk_id for chunk in result.chunks):
            raise ServiceError("invalid_citation", "Citation is unavailable", 422)
        for provider, branch in zip(self.providers, result.branches, strict=True):
            if any(chunk.chunk_id == citation.chunk_id for chunk in branch.chunks):
                chunk = provider.citation(branch, citation)
                self.verify(result)
                return chunk
        raise ServiceError("invalid_citation", "Citation is unavailable", 422)


def validate_branches(
    branches: tuple[SearchResult, SearchResult], request: QueryRequest, principal: Principal
) -> None:
    """Refuse cross-snapshot fusion and conflicting records sharing an immutable chunk ID."""
    seen: dict[str, Chunk] = {}
    for branch in branches:
        if (
            branch.request != request
            or branch.principal != principal
            or branch.catalog_revision != branches[0].catalog_revision
        ):
            raise ServiceError(
                "search_scope_changed", "Search scope changed; retry the request", 409
            )
        for chunk in branch.chunks:
            if chunk.chunk_id in seen and seen[chunk.chunk_id] != chunk:
                raise ServiceError("conflicting_evidence", "Search evidence is inconsistent", 409)
            seen[chunk.chunk_id] = chunk
