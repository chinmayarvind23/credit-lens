"""Bound reranking while retaining every underlying authorization checkpoint."""

from dataclasses import dataclass

from creditlens.domain import Chunk, Citation, Principal, QueryRequest
from creditlens.errors import ServiceError
from creditlens.hybrid_provider import CanonicalSearchProvider
from creditlens.search_provider import Ranker, SearchResult, validate_ranking


@dataclass(frozen=True)
class RerankedResult(SearchResult):
    """Keep the original branch results available for final checks and exact citations."""

    source: SearchResult


class RerankProvider:
    """A required reranker can reorder a bounded canonical pool, never expand its authority."""

    def __init__(
        self,
        provider: CanonicalSearchProvider,
        ranker: Ranker,
        *,
        mode: str,
        candidates: int = 40,
    ) -> None:
        """Use the measured 40-candidate baseline; model revision belongs in the explicit mode."""
        if not 1 <= candidates <= 100 or not mode or len(mode) > 200:
            raise ValueError("Reranking requires a bounded candidate count and mode")
        self.provider = provider
        self.catalog = provider.catalog
        self.ranker = ranker
        self.mode = mode
        self.candidates = candidates

    def _source(self, source: SearchResult, request: QueryRequest, principal: Principal) -> None:
        """Rehydrate current scope before model access, even if a branch returned bad records."""
        if self.provider.catalog is not self.catalog:
            raise ServiceError("search_scope_changed", "Search scope changed; retry", 409)
        self.provider.verify(source)
        allowed, revision = self.catalog.snapshot(
            principal, request.borrower_id, request.effective_at
        )
        if (
            source.request != request
            or source.principal != principal
            or source.catalog_revision != revision
        ):
            raise ServiceError("invalid_search_result", "Search result is unavailable", 503)
        validate_ranking(source.chunks, allowed, self.candidates)
        self.catalog.verify_revision(revision)

    def search(
        self, request: QueryRequest, principal: Principal, limit: int = 10
    ) -> RerankedResult:
        """Reject invalid candidates before scoring and reject revocation after scoring."""
        if not 1 <= limit <= min(10, self.candidates):
            raise ValueError("Reranked output must fit the ten-result workflow budget")
        if self.provider.catalog is not self.catalog:
            raise ServiceError("search_scope_changed", "Search scope changed; retry", 409)
        source = self.provider.search(request, principal, self.candidates)
        self._source(source, request, principal)
        ranking = self.ranker(request.question, source.chunks, limit)
        validate_ranking(ranking, source.chunks, limit)
        result = RerankedResult(
            ranking,
            principal,
            request,
            source.catalog_revision,
            f"reranked:{self.mode}:{source.provider_mode}",
            source,
        )
        self.verify(result)
        return result

    def verify(self, result: SearchResult) -> None:
        """Retain source freshness and subset validation through the final workflow boundary."""
        if not isinstance(result, RerankedResult):
            raise ServiceError("invalid_search_result", "Search result is unavailable", 503)
        self._source(result.source, result.request, result.principal)
        if result.catalog_revision != result.source.catalog_revision:
            raise ServiceError("invalid_search_result", "Search result is unavailable", 503)
        validate_ranking(result.chunks, result.source.chunks, min(10, self.candidates))

    def citation(self, result: SearchResult, citation: Citation) -> Chunk:
        """Only reranked output may be cited; its original provider checks exact provenance."""
        self.verify(result)
        if not isinstance(result, RerankedResult):
            raise TypeError("Expected a reranked result")
        if not any(chunk.chunk_id == citation.chunk_id for chunk in result.chunks):
            raise ServiceError("invalid_citation", "Citation is unavailable", 422)
        chunk = self.provider.citation(result.source, citation)
        self.verify(result)
        return chunk
