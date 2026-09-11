"""Optional retrieval caching reauthorizes and hydrates signed IDs from current canonical pages."""

import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from time import time
from typing import Literal, Protocol

from creditlens.cache import ByteCache, CachedIDs, CacheUnavailable, read_entry, sign_entry
from creditlens.domain import Chunk, Citation, Principal, QueryRequest
from creditlens.errors import ServiceError
from creditlens.retrieval import CanonicalCatalog
from creditlens.search_provider import SearchProvider, SearchResult
from creditlens.storage import GrantStore

CacheState = Literal["hit", "miss", "invalid", "unavailable"]


class CanonicalProvider(SearchProvider, Protocol):
    """The cache must share its wrapped provider's authoritative catalog instance."""

    catalog: CanonicalCatalog


@dataclass(frozen=True)
class CachedResult(SearchResult):
    """Expose acceleration state without confusing a cache hit with another remote search."""

    cache_state: CacheState


class RetrievalCache:
    """A failed cache reads through to the same provider; current permissions are never cached."""

    def __init__(
        self,
        provider: CanonicalProvider,
        store: GrantStore,
        backend: ByteCache,
        secret: bytes,
        provider_revision: str,
        *,
        ttl: int = 60,
        clock: Callable[[], float] = time,
    ) -> None:
        """Bind one provider/index/model revision and a runtime signing key to a finite TTL."""
        if len(secret) < 32 or not provider_revision or not 1 <= ttl <= 3600:
            raise ValueError("Cache requires a strong key, provider revision and bounded TTL")
        self.provider = provider
        self.catalog = provider.catalog
        self.store = store
        self.backend = backend
        self._secret = secret
        self.provider_revision = provider_revision
        self.ttl = ttl
        self.clock = clock

    def key(self, request: QueryRequest, principal: Principal, revision: int, limit: int) -> str:
        """Hide query/scope data while binding request, permission, catalog and index dimensions."""
        identity = {
            "schema": 1,
            "principal": principal.model_dump(mode="json"),
            "request": request.model_dump(mode="json"),
            "catalog_version": self.catalog.version,
            "catalog_revision": revision,
            "provider_revision": self.provider_revision,
            "limit": limit,
        }
        digest = hmac.new(
            self._secret, json.dumps(identity, sort_keys=True).encode(), hashlib.sha256
        ).hexdigest()
        return f"creditlens:retrieval:v1:{digest}"

    def verify(self, result: SearchResult) -> None:
        """Recheck authority and current grants on every hit, citation and final consumer use."""
        if not isinstance(result, CachedResult):
            raise ServiceError("invalid_search_result", "Search result is unavailable", 422)
        if self.provider.catalog is not self.catalog:
            raise ServiceError("access_changed", "Access changed; retry the request", 409)
        self.catalog.verify_revision(result.catalog_revision)
        if self.store.resolve(result.principal.subject) != result.principal:
            raise ServiceError("access_changed", "Access changed; retry the request", 409)

    def search(self, request: QueryRequest, principal: Principal, limit: int = 10) -> CachedResult:
        """Authorize before Redis, then replace IDs with current canonical records."""
        if not 1 <= limit <= 100:
            raise ValueError("Search limit must be between 1 and 100")
        current = self.store.resolve(principal.subject)
        candidates, revision = self.catalog.snapshot(
            current, request.borrower_id, request.effective_at
        )
        checkpoint = CachedResult((), principal, request, revision, self.provider_revision, "miss")
        self.verify(checkpoint)
        key = self.key(request, principal, revision, limit)
        allowed = {c.chunk_id: c for c in candidates}
        cached, state = self._read(key, allowed, limit)
        if cached is not None:
            result = CachedResult(
                tuple(allowed[c] for c in cached.chunk_ids),
                principal,
                request,
                revision,
                cached.provider_mode,
                "hit",
            )
            self.verify(result)
            return result
        self.verify(checkpoint)
        fresh = self.provider.search(request, principal, limit)
        self.provider.verify(fresh)
        self._validate_fresh(fresh, checkpoint, allowed, limit)
        entry = CachedIDs(
            key=key,
            expires_at=self.clock() + self.ttl,
            chunk_ids=tuple(c.chunk_id for c in fresh.chunks),
            provider_mode=fresh.provider_mode,
        )
        try:
            self.backend.put(key, sign_entry(entry, self._secret), self.ttl)
        except CacheUnavailable:
            state = "unavailable"
        result = CachedResult(
            fresh.chunks, principal, request, revision, fresh.provider_mode, state
        )
        self.verify(result)
        return result

    def _read(
        self, key: str, allowed: dict[str, Chunk], limit: int
    ) -> tuple[CachedIDs | None, CacheState]:
        """Bad cache bytes cause an observable miss; they cannot become unverified evidence."""
        try:
            value = self.backend.get(key)
        except CacheUnavailable:
            return None, "unavailable"
        if value is None:
            return None, "miss"
        try:
            entry = read_entry(value, key, self._secret, self.clock())
            if len(entry.chunk_ids) > limit or any(c not in allowed for c in entry.chunk_ids):
                raise ValueError("Cached IDs do not match the current scope")
            return entry, "hit"
        except (ValueError, RecursionError):
            return None, "invalid"

    def _validate_fresh(
        self, fresh: SearchResult, checkpoint: CachedResult, allowed: dict[str, Chunk], limit: int
    ) -> None:
        """A provider bug cannot seed the cache with mismatched scope or altered canonical text."""
        self.verify(checkpoint)
        valid = (
            fresh.principal == checkpoint.principal
            and fresh.request == checkpoint.request
            and fresh.catalog_revision == checkpoint.catalog_revision
            and len(fresh.chunks) <= limit
            and len({c.chunk_id for c in fresh.chunks}) == len(fresh.chunks)
            and all(allowed.get(c.chunk_id) == c for c in fresh.chunks)
        )
        if not valid:
            raise ServiceError("invalid_search_result", "Search result is unavailable", 503)

    def citation(self, result: SearchResult, citation: Citation) -> Chunk:
        """Resolve only evidence in this result and in the current authorized catalog snapshot."""
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
