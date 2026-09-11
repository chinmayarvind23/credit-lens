"""The local BM25 control implements the same current-evidence provider contract."""

from creditlens.domain import Chunk, Citation, Principal, QueryRequest
from creditlens.errors import ServiceError
from creditlens.retrieval import EvidenceCatalog, lexical_rank
from creditlens.search_provider import SearchResult
from creditlens.storage import GrantStore


class LocalSearchProvider:
    """Keep local cache and provider comparisons explicit without inventing remote execution."""

    def __init__(self, catalog: EvidenceCatalog, store: GrantStore) -> None:
        """Share the authoritative catalog and SQL grant store with downstream consumers."""
        self.catalog = catalog
        self.store = store

    def verify(self, result: SearchResult) -> None:
        """Reject current revocation even when the original search has already completed."""
        self.catalog.verify_revision(result.catalog_revision)
        if self.store.resolve(result.principal.subject) != result.principal:
            raise ServiceError("access_changed", "Access changed; retry the request", 409)

    def search(self, request: QueryRequest, principal: Principal, limit: int = 10) -> SearchResult:
        """Select current authorized candidates before the local lexical scorer sees text."""
        current = self.store.resolve(principal.subject)
        candidates, revision = self.catalog.snapshot(
            current, request.borrower_id, request.effective_at
        )
        checkpoint = SearchResult((), principal, request, revision, "local-bm25")
        self.verify(checkpoint)
        ranking = lexical_rank(request.question, candidates, limit)
        result = SearchResult(ranking, principal, request, revision, "local-bm25")
        self.verify(result)
        return result

    def citation(self, result: SearchResult, citation: Citation) -> Chunk:
        """Only exact retrieved evidence still present in the current snapshot can be cited."""
        self.verify(result)
        candidates, revision = self.catalog.snapshot(
            result.principal, result.request.borrower_id, result.request.effective_at
        )
        chunk = next((c for c in result.chunks if c.chunk_id == citation.chunk_id), None)
        if (
            chunk is None
            or chunk not in candidates
            or revision != result.catalog_revision
            or (chunk.document_id, chunk.document_version, chunk.page)
            != (citation.document_id, citation.document_version, citation.page)
        ):
            raise ServiceError("invalid_citation", "Citation is unavailable", 422)
        self.verify(result)
        return chunk
