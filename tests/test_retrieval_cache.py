"""Cache faults and malicious entries cannot bypass current canonical authorization."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date

import pytest
from sqlalchemy import update

from creditlens.cache import CachedIDs, CacheUnavailable, sign_entry
from creditlens.citations import cite
from creditlens.corpus import build_demo_pages
from creditlens.domain import Chunk, Citation, Principal, QueryRequest
from creditlens.errors import ServiceError
from creditlens.local_search import LocalSearchProvider
from creditlens.retrieval import EvidenceCatalog, lexical_rank
from creditlens.retrieval_cache import RetrievalCache
from creditlens.search_provider import SearchResult
from creditlens.storage import GrantStore, grants, open_database


class MemoryBytes:
    """A transport fault double leaves expiry and authorization to the real cache wrapper."""

    def __init__(self) -> None:
        """Track transport work to prove denied identities never consult cached evidence."""
        self.values: dict[str, bytes] = {}
        self.reads = 0
        self.writes = 0
        self.fail_read = False
        self.fail_write = False
        self.after_read: Callable[[], None] | None = None

    def get(self, key: str) -> bytes | None:
        """Inject outage or concurrent revocation at the cache network boundary."""
        self.reads += 1
        if self.fail_read:
            raise CacheUnavailable("offline")
        value = self.values.get(key)
        if self.after_read:
            self.after_read()
        return value

    def put(self, key: str, value: bytes, ttl: int) -> None:
        """Retain bytes for tampering tests without simulating Redis server expiry."""
        self.writes += 1
        if self.fail_write:
            raise CacheUnavailable("offline")
        self.values[key] = value


class CountingProvider:
    """The test's local BM25 control counts ranking and can inject an invalid provider result."""

    def __init__(self, store: GrantStore) -> None:
        """Use real canonical pages and current SQL grants for repeatable cache scenarios."""
        self.catalog = EvidenceCatalog(build_demo_pages())
        self.store = store
        self.calls = 0
        self.replacement: SearchResult | None = None

    def search(self, request: QueryRequest, principal: Principal, limit: int = 10) -> SearchResult:
        """Count local ranking to distinguish a cache hit from recomputation."""
        self.calls += 1
        if self.replacement:
            return self.replacement
        candidates, revision = self.catalog.snapshot(
            principal, request.borrower_id, request.effective_at
        )
        return SearchResult(
            lexical_rank(request.question, candidates, limit),
            principal,
            request,
            revision,
            "test-local-bm25",
        )

    def verify(self, result: SearchResult) -> None:
        """Exercise the same current-grant and catalog checkpoint used by real providers."""
        self.catalog.verify_revision(result.catalog_revision)
        if self.store.resolve(result.principal.subject) != result.principal:
            raise ServiceError("access_changed", "Changed", 409)

    def citation(self, result: SearchResult, citation: Citation) -> Chunk:
        """The cache owns citation validation, so delegation here would invalidate this test."""
        raise AssertionError("Cached citations must resolve through current canonical evidence")


@dataclass
class Rig:
    """Own one isolated SQL store and controlled clock per cache test."""

    cache: RetrievalCache
    backend: MemoryBytes
    provider: CountingProvider
    principal: Principal
    request: QueryRequest
    now: list[float]
    secret: bytes


@pytest.fixture
def rig() -> Iterator[Rig]:
    """Keep permission mutations isolated and dispose the shared SQLite memory connection."""
    engine = open_database("sqlite:///:memory:")
    store = GrantStore(engine)
    store.seed_demo()
    provider, backend = CountingProvider(store), MemoryBytes()
    now, secret = [1000.0], b"x" * 32
    cache = RetrievalCache(provider, store, backend, secret, "local-bm25-v1", clock=lambda: now[0])
    yield Rig(
        cache,
        backend,
        provider,
        store.resolve("synthetic-demo"),
        QueryRequest(
            borrower_id="borrower-001",
            question="debt service coverage",
            effective_at=date(2026, 9, 11),
        ),
        now,
        secret,
    )
    engine.dispose()


def test_cache_hit_and_citation(rig: Rig) -> None:
    """A warm request skips ranking but rehydrates identical source records and valid citations."""
    first = rig.cache.search(rig.request, rig.principal)
    second = rig.cache.search(rig.request, rig.principal)
    assert first.cache_state == "miss" and second.cache_state == "hit"
    assert first.chunks == second.chunks and rig.provider.calls == 1
    assert rig.cache.citation(second, cite(second.chunks[0])) == second.chunks[0]
    with pytest.raises(ServiceError, match="invalid_citation"):
        rig.cache.citation(second, cite(second.chunks[0]).model_copy(update={"page": 999}))


def test_keys_bind_scope_date_versions_and_query(rig: Rig) -> None:
    """Changing any authorization or retrieval identity prevents cross-request cache reuse."""
    key = rig.cache.key(rig.request, rig.principal, 1, 10)
    keys = {key}
    for field, value in (
        ("question", "another question"),
        ("borrower_id", "borrower-002"),
        ("effective_at", date(2025, 1, 1)),
    ):
        keys.add(rig.cache.key(rig.request.model_copy(update={field: value}), rig.principal, 1, 10))
    for field, value in (
        ("subject", "other"),
        ("tenant_id", "other"),
        ("role", "reviewer"),
        ("revision", 2),
        ("acl_groups", ("credit-officer",)),
        ("borrower_ids", ("borrower-002",)),
    ):
        keys.add(rig.cache.key(rig.request, rig.principal.model_copy(update={field: value}), 1, 10))
    keys.add(rig.cache.key(rig.request, rig.principal, 2, 10))
    keys.add(rig.cache.key(rig.request, rig.principal, 1, 20))
    rig.cache.provider_revision = "new-index"
    keys.add(rig.cache.key(rig.request, rig.principal, 1, 10))
    assert len(keys) == 13
    assert all("borrower-001" not in key and rig.request.question not in key for key in keys)


def test_expiry_recomputes_without_changing_answer(rig: Rig) -> None:
    """Signed expiry protects readers even if a faulty backend retains old values after TTL."""
    first = rig.cache.search(rig.request, rig.principal)
    rig.now[0] += 61
    next_result = rig.cache.search(rig.request, rig.principal)
    assert next_result.cache_state == "invalid"
    assert next_result.chunks == first.chunks and rig.provider.calls == 2


@pytest.mark.parametrize("fault", ["fail_read", "fail_write"])
def test_cache_outage_preserves_authorized_fallback(rig: Rig, fault: str) -> None:
    """Redis faults are observable while the original permission-aware provider still runs."""
    setattr(rig.backend, fault, True)
    result = rig.cache.search(rig.request, rig.principal)
    assert result.cache_state == "unavailable" and result.chunks and rig.provider.calls == 1


def test_revoked_grant_never_reads_warm_cache(rig: Rig) -> None:
    """Disabling a current SQL grant prevents any cache access despite a valid old principal."""
    rig.cache.search(rig.request, rig.principal)
    with rig.provider.store.engine.begin() as connection:
        connection.execute(update(grants).values(enabled=False))
    reads = rig.backend.reads
    with pytest.raises(ServiceError, match="access_denied"):
        rig.cache.search(rig.request, rig.principal)
    assert rig.backend.reads == reads and rig.provider.calls == 1


def test_page_revocation_invalidates_warm_ids(rig: Rig) -> None:
    """The epoch changes cache identity and prevents use of a removed canonical page."""
    first = rig.cache.search(rig.request, rig.principal)
    rig.provider.catalog.revoke(first.chunks[0].chunk_id)
    next_result = rig.cache.search(rig.request, rig.principal)
    assert first.chunks[0] not in next_result.chunks and next_result.cache_state == "miss"
    with pytest.raises(ServiceError, match="evidence_changed"):
        rig.cache.citation(first, cite(first.chunks[0]))


def test_revocation_during_cache_read_fails(rig: Rig) -> None:
    """A hit cannot escape if permissions change while the cache network call is in flight."""
    first = rig.cache.search(rig.request, rig.principal)
    rig.backend.after_read = lambda: rig.provider.catalog.revoke(first.chunks[0].chunk_id)
    with pytest.raises(ServiceError, match="evidence_changed"):
        rig.cache.search(rig.request, rig.principal)
    assert rig.provider.calls == 1


@pytest.mark.parametrize(
    "value",
    [b"broken", b'{"payload":"x","signature":"bad"}', b"x" * 32769],
    ids=["malformed-json", "bad-signature", "oversized"],
)
def test_bad_cache_bytes_recompute(rig: Rig, value: bytes) -> None:
    """Malformed, invalid-signature and oversized values cannot seed evidence or crash the API."""
    rig.backend.values[rig.cache.key(rig.request, rig.principal, 1, 10)] = value
    assert rig.cache.search(rig.request, rig.principal).cache_state == "invalid"
    assert rig.provider.calls == 1


def test_signed_forbidden_ids_still_fail_scope(rig: Rig) -> None:
    """Even a correctly signed entry cannot replace current canonical authorization."""
    key = rig.cache.key(rig.request, rig.principal, 1, 10)
    entry = CachedIDs(key=key, expires_at=1100, chunk_ids=("f" * 64,), provider_mode="injected")
    rig.backend.values[key] = sign_entry(entry, rig.secret)
    result = rig.cache.search(rig.request, rig.principal)
    assert result.cache_state == "invalid" and result.provider_mode == "test-local-bm25"


def test_fresh_provider_cannot_poison_cache(rig: Rig) -> None:
    """Reject altered text under a real ID before a buggy provider can persist it in Redis."""
    fresh = rig.provider.search(rig.request, rig.principal)
    altered = fresh.chunks[0].model_copy(update={"text": "Invented text"})
    rig.provider.replacement = SearchResult((altered,), rig.principal, rig.request, 1, "bad")
    with pytest.raises(ServiceError, match="invalid_search_result"):
        rig.cache.search(rig.request, rig.principal)
    assert rig.backend.writes == 0


@pytest.mark.parametrize(
    ("secret", "revision", "ttl"), [(b"short", "v1", 60), (b"x" * 32, "", 60), (b"x" * 32, "v1", 0)]
)
def test_invalid_cache_configuration(rig: Rig, secret: bytes, revision: str, ttl: int) -> None:
    """Reject weak signing keys and unversioned or unbounded cache configuration at startup."""
    with pytest.raises(ValueError):
        RetrievalCache(rig.provider, rig.provider.store, rig.backend, secret, revision, ttl=ttl)


def test_cache_rejects_stale_grants_and_provider_swap(rig: Rig) -> None:
    """Neither coincident epochs nor a previously valid principal replace current authority."""
    with pytest.raises(ValueError):
        rig.cache.search(rig.request, rig.principal, 0)
    plain = rig.provider.search(rig.request, rig.principal)
    with pytest.raises(ServiceError, match="invalid_search_result"):
        rig.cache.verify(plain)
    with rig.provider.store.engine.begin() as connection:
        connection.execute(update(grants).values(revision=2))
    with pytest.raises(ServiceError, match="access_changed"):
        rig.cache.search(rig.request, rig.principal)
    current = rig.provider.store.resolve(rig.principal.subject)
    rig.provider.catalog = EvidenceCatalog(build_demo_pages())
    with pytest.raises(ServiceError, match="access_changed"):
        rig.cache.search(rig.request, current)
    assert rig.backend.reads == 0


def test_real_local_provider_contract(rig: Rig) -> None:
    """The cache also wraps the actual BM25 provider with exact citation and revocation checks."""
    provider = LocalSearchProvider(rig.provider.catalog, rig.provider.store)
    cache = RetrievalCache(provider, rig.provider.store, rig.backend, rig.secret, "local-v1")
    result = cache.search(rig.request, rig.principal)
    assert result.provider_mode == "local-bm25"
    assert provider.citation(result, cite(result.chunks[0])) == result.chunks[0]
    with pytest.raises(ServiceError, match="invalid_citation"):
        provider.citation(result, cite(result.chunks[0]).model_copy(update={"page": 999}))
    with rig.provider.store.engine.begin() as connection:
        connection.execute(update(grants).values(revision=2))
    with pytest.raises(ServiceError, match="access_changed"):
        provider.search(rig.request, rig.principal)
