"""Search providers return canonical evidence with an explicit freshness checkpoint."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from creditlens.domain import Chunk, Citation, Principal, QueryRequest
from creditlens.errors import ServiceError

Ranker = Callable[[str, tuple[Chunk, ...], int], tuple[Chunk, ...]]


def validate_ranking(ranking: tuple[Chunk, ...], candidates: tuple[Chunk, ...], limit: int) -> None:
    """A scorer may order canonical evidence, never add, alter or duplicate its records."""
    allowed = {chunk.chunk_id: chunk for chunk in candidates}
    if (
        not isinstance(ranking, tuple)
        or len(ranking) > limit
        or any(not isinstance(chunk, Chunk) for chunk in ranking)
        or len({chunk.chunk_id for chunk in ranking}) != len(ranking)
        or any(allowed.get(chunk.chunk_id) != chunk for chunk in ranking)
    ):
        raise ServiceError("invalid_search_result", "Search result is unavailable", 503)


@dataclass(frozen=True)
class SearchResult:
    """Consumers must verify this snapshot again before audit and response publication."""

    chunks: tuple[Chunk, ...]
    principal: Principal
    request: QueryRequest
    catalog_revision: int
    provider_mode: str


class SearchProvider(Protocol):
    """Keep remote ranking separate from deterministic finance and packet generation."""

    def search(self, request: QueryRequest, principal: Principal, limit: int = 10) -> SearchResult:
        """Authorize before ranking and return only canonical, currently scoped chunks."""
        ...

    def verify(self, result: SearchResult) -> None:
        """Reject a result whose grants or catalog changed during downstream work."""
        ...

    def citation(self, result: SearchResult, citation: Citation) -> Chunk:
        """Resolve exact cited provenance and recheck access at the point of use."""
        ...
