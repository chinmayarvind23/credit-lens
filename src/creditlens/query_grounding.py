"""Bind retrieval queries to authorized borrower metadata while preserving original requests."""

import re
from dataclasses import dataclass

from creditlens.citations import cite
from creditlens.domain import Chunk, Citation, Principal, QueryRequest
from creditlens.errors import ServiceError
from creditlens.intent import question_words
from creditlens.retrieval_cache import CanonicalProvider
from creditlens.search_provider import SearchResult, validate_ranking
from creditlens.storage import GrantStore

GROUNDING_VERSION = "borrower-context-v2"
CONTEXT_WORDS = frozenset(
    "borrower package packet cash numerator denominator missing unprovided documented "
    "discrepancy discrepancies reconcile reconciliation".split()
)


def grounded_query(
    question: str, borrower_id: str, allowed: tuple[Chunk, ...]
) -> tuple[str, bool, str]:
    """Use one canonical application label; ambiguity or bad metadata leaves the question intact."""
    names = {
        c.title.removesuffix(" / application")
        for c in allowed
        if c.borrower_id == borrower_id
        and c.document_kind == "application"
        and c.title.endswith(" / application")
    }
    if len(names) != 1:
        return question, False, "missing_or_ambiguous_name"
    name = names.pop()
    if not name.strip() or not re.fullmatch(r"[\w .,&'()-]{1,120}", name):
        return question, False, "invalid_name"
    expanded = f"Borrower: {name}. {question}"
    if len(expanded) > 2000:
        return question, False, "question_budget"
    selected = bool(question_words(question) & CONTEXT_WORDS)
    return expanded, selected, "borrower_context" if selected else "general_question"


def model_request(request: QueryRequest, allowed: tuple[Chunk, ...]) -> QueryRequest:
    """Change only ranker text; original borrower, date and workflow question stay intact."""
    expanded, selected, _ = grounded_query(request.question, request.borrower_id, allowed)
    return request.model_copy(update={"question": expanded}) if selected else request


@dataclass(frozen=True)
class GroundedResult(SearchResult):
    """Retain the actual model request separately from the request seen by workflow and cache."""

    source: SearchResult


class GroundedProvider:
    """Resolve names under current grants, then delegate all ranking to the existing provider."""

    def __init__(self, provider: CanonicalProvider, store: GrantStore) -> None:
        """Share one catalog authority and grant resolver instead of caching borrower identity."""
        self.provider = provider
        self.catalog = provider.catalog
        self.store = store

    def _authority(self, principal: Principal) -> None:
        """Metadata lookup cannot use a stale principal or a replaced provider catalog."""
        if (
            self.provider.catalog is not self.catalog
            or self.store.resolve(principal.subject) != principal
        ):
            raise ServiceError("access_changed", "Access changed; retry the request", 409)

    def _snapshot(
        self, request: QueryRequest, principal: Principal
    ) -> tuple[tuple[Chunk, ...], int]:
        """Bracket name resolution with current grants and a canonical catalog revision."""
        self._authority(principal)
        allowed, revision = self.catalog.snapshot(
            principal, request.borrower_id, request.effective_at
        )
        self._authority(principal)
        self.catalog.verify_revision(revision)
        return allowed, revision

    def search(
        self, request: QueryRequest, principal: Principal, limit: int = 10
    ) -> GroundedResult:
        """Transform only authorized model input and bind its output to the original request."""
        if not 1 <= limit <= 10:
            raise ValueError("Grounded results must fit the ten-result workflow budget")
        allowed, revision = self._snapshot(request, principal)
        source = self.provider.search(model_request(request, allowed), principal, limit)
        validate_ranking(source.chunks, allowed, limit)
        result = GroundedResult(
            source.chunks,
            principal,
            request,
            revision,
            f"{GROUNDING_VERSION}:{source.provider_mode}",
            source,
        )
        self.verify(result)
        return result

    def verify(self, result: SearchResult) -> None:
        """Reconstruct the permitted rewrite so forged requests or changed names cannot survive."""
        if not isinstance(result, GroundedResult):
            raise ServiceError("invalid_search_result", "Search result is unavailable", 503)
        allowed, revision = self._snapshot(result.request, result.principal)
        source = result.source
        if (
            source.request != model_request(result.request, allowed)
            or source.principal != result.principal
            or source.catalog_revision != revision
            or result.catalog_revision != revision
            or result.chunks != source.chunks
            or result.provider_mode != f"{GROUNDING_VERSION}:{source.provider_mode}"
        ):
            raise ServiceError("invalid_search_result", "Search result is unavailable", 503)
        validate_ranking(result.chunks, allowed, 10)
        self.provider.verify(source)
        self._authority(result.principal)
        self.catalog.verify_revision(revision)

    def citation(self, result: SearchResult, citation: Citation) -> Chunk:
        """Delegate through the retained source, enforcing exact output membership on return."""
        self.verify(result)
        if not isinstance(result, GroundedResult):
            raise TypeError("Expected a grounded result")
        chunk = self.provider.citation(result.source, citation)
        validate_ranking((chunk,), result.chunks, 1)
        if cite(chunk) != citation:
            raise ServiceError("invalid_citation", "Citation is unavailable", 422)
        self.verify(result)
        return chunk
