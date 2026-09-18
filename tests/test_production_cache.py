"""Production cache wrapping must preserve dependency health and canonical authority."""

import os
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import date
from secrets import token_bytes
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import update

from creditlens.cache import CacheUnavailable, RedisBytes
from creditlens.citations import cite
from creditlens.corpus import build_demo_pages
from creditlens.domain import Principal, QueryRequest
from creditlens.errors import ServiceError
from creditlens.local_search import LocalSearchProvider
from creditlens.retrieval import EvidenceCatalog
from creditlens.retrieval_cache import RetrievalCache
from creditlens.search_provider import SearchResult
from creditlens.settings import Settings
from creditlens.storage import GrantStore, grants, open_database
from creditlens.weaviate_provider import WeaviateHybridProvider


class ControlledBytes:
    """Exercise optional-cache faults without replacing the canonical search provider."""

    def __init__(self) -> None:
        """Track IO so dependency readiness cannot accidentally require Redis."""
        self.values: dict[str, bytes] = {}
        self.reads = 0
        self.writes = 0
        self.available = True

    def get(self, key: str) -> bytes | None:
        """Allow an optional transport outage while retaining signed cache semantics."""
        self.reads += 1
        if not self.available:
            raise CacheUnavailable("Cache read unavailable")
        return self.values.get(key)

    def put(self, key: str, value: bytes, ttl: int) -> None:
        """Count writes to distinguish a dependency failure from a fresh search result."""
        self.writes += 1
        if not self.available:
            raise CacheUnavailable("Cache write unavailable")
        self.values[key] = value


class RequiredProvider(LocalSearchProvider):
    """Use real canonical BM25 behavior with an independently failing required dependency."""

    def __init__(self, catalog: EvidenceCatalog, store: GrantStore) -> None:
        """Keep readiness faults separate from Redis and ranking counters."""
        super().__init__(catalog, store)
        self.available = True
        self.probes = 0
        self.searches = 0

    def check_ready(self) -> None:
        """Represent the selected required remote provider's curated health failure."""
        self.probes += 1
        if not self.available:
            raise ServiceError("search_unavailable", "Required search is unavailable", 503)

    def search(self, request: QueryRequest, principal: Principal, limit: int = 10) -> SearchResult:
        """Fail actual work independently from the application's explicit readiness request."""
        self.searches += 1
        if not self.available:
            raise ServiceError("search_unavailable", "Required search is unavailable", 503)
        return super().search(request, principal, limit)


@dataclass
class CacheRig:
    """Keep canonical authority, dependency faults and test cache keys isolated."""

    provider: RequiredProvider
    store: GrantStore
    catalog: EvidenceCatalog
    backend: ControlledBytes
    cache: RetrievalCache
    principal: Principal
    request: QueryRequest
    secret: bytes


@pytest.fixture
def rig() -> Iterator[CacheRig]:
    """Use current SQL grants and real canonical pages without a managed service dependency."""
    engine = open_database("sqlite:///:memory:")
    store = GrantStore(engine)
    store.seed_demo()
    catalog = EvidenceCatalog(build_demo_pages())
    provider, backend, secret = RequiredProvider(catalog, store), ControlledBytes(), token_bytes(32)
    cache = RetrievalCache(provider, store, backend, secret, "governed-search-v1")
    try:
        yield CacheRig(
            provider,
            store,
            catalog,
            backend,
            cache,
            store.resolve("synthetic-demo"),
            QueryRequest(
                borrower_id="borrower-001",
                question="debt service coverage",
                effective_at=date(2026, 9, 11),
            ),
            secret,
        )
    finally:
        engine.dispose()


def test_cache_readiness_forwards_required_health_without_redis(rig: CacheRig) -> None:
    """A warmed cache and an optional Redis outage cannot hide required dependency health."""
    rig.cache.search(rig.request, rig.principal)
    rig.cache.search(rig.request, rig.principal)
    reads, writes = rig.backend.reads, rig.backend.writes
    rig.backend.available = False
    rig.cache.check_ready()
    assert rig.provider.probes == 1
    rig.provider.available = False
    with pytest.raises(ServiceError, match="search_unavailable"):
        rig.cache.check_ready()
    assert rig.provider.probes == 2
    assert (rig.backend.reads, rig.backend.writes) == (reads, writes)


def test_nested_readiness_and_provider_without_probe(rig: CacheRig) -> None:
    """Optional probe delegation composes without breaking existing canonical providers."""
    outer = RetrievalCache(rig.cache, rig.store, rig.backend, rig.secret, "outer-v1")
    outer.check_ready()
    assert rig.provider.probes == 1
    plain = RetrievalCache(
        LocalSearchProvider(rig.catalog, rig.store), rig.store, rig.backend, rig.secret, "plain-v1"
    )
    plain.check_ready()
    assert rig.backend.reads == rig.backend.writes == 0
    rig.provider.available = False
    with pytest.raises(ServiceError, match="search_unavailable"):
        outer.check_ready()


def test_redis_outage_does_not_downgrade_required_provider_failure(rig: CacheRig) -> None:
    """Read-through remains observable while remote search failures still fail the request."""
    rig.backend.available = False
    fresh = rig.cache.search(rig.request, rig.principal)
    assert fresh.cache_state == "unavailable" and fresh.chunks
    writes = rig.backend.writes
    rig.provider.available = False
    with pytest.raises(ServiceError, match="search_unavailable"):
        rig.cache.search(rig.request, rig.principal)
    assert rig.backend.writes == writes


def test_provider_identity_migration_does_not_reuse_old_rankings(rig: CacheRig) -> None:
    """One Redis namespace can safely hold distinct pinned model/index configurations."""
    old = rig.cache.search(rig.request, rig.principal)
    migrated = RetrievalCache(
        rig.provider, rig.store, rig.backend, rig.secret, "governed-search-v2-model-or-index-change"
    )
    new = migrated.search(rig.request, rig.principal)
    assert old.cache_state == new.cache_state == "miss"
    assert rig.provider.searches == 2 and len(rig.backend.values) == 2
    assert migrated.search(rig.request, rig.principal).cache_state == "hit"
    assert rig.cache.search(rig.request, rig.principal).cache_state == "hit"
    with rig.store.engine.begin() as connection:
        connection.execute(update(grants).values(enabled=False))
    reads = rig.backend.reads
    with pytest.raises(ServiceError, match="access_denied"):
        migrated.search(rig.request, rig.principal)
    assert rig.backend.reads == reads


@pytest.mark.skipif(
    not os.environ.get("CREDITLENS_TEST_REDIS_URL"),
    reason="Explicit isolated Redis integration URL required",
)
def test_real_redis_wrapped_dependency_and_canonical_recovery(rig: CacheRig) -> None:
    """Actual Redis bytes preserve canonical revocation and required-provider readiness."""
    backend = RedisBytes(os.environ["CREDITLENS_TEST_REDIS_URL"])
    cache = RetrievalCache(rig.provider, rig.store, backend, rig.secret, "governed-redis-v1")
    try:
        first = cache.search(rig.request, rig.principal)
        hit = cache.search(rig.request, rig.principal)
        assert first.cache_state == "miss" and hit.cache_state == "hit"
        assert hit.chunks == first.chunks and rig.provider.searches == 1
        cache.check_ready()
        rig.provider.available = False
        with pytest.raises(ServiceError, match="search_unavailable"):
            cache.check_ready()
        rig.provider.available = True
        backend.put(cache.key(rig.request, rig.principal, hit.catalog_revision, 10), b"tamper", 60)
        assert cache.search(rig.request, rig.principal).cache_state == "invalid"
        rig.catalog.revoke(first.chunks[0].chunk_id)
        refreshed = cache.search(rig.request, rig.principal)
        assert refreshed.cache_state == "miss" and first.chunks[0] not in refreshed.chunks
        with pytest.raises(ServiceError, match="evidence_changed"):
            cache.citation(hit, cite(hit.chunks[0]))
    finally:
        backend.close()


def governed_config() -> Settings:
    """Validate production cache selection without contacting the synthetic endpoint names."""
    return Settings(
        mode="production",
        production_search="weaviate",
        catalog_backend="postgres",
        database_url="postgresql+psycopg://unused:unused@127.0.0.1/unused",
        governed_catalog_id="governed-cache-test",
        issuer="https://cognito-idp.us-east-1.amazonaws.com/test",
        client_id="client",
        weaviate_url="https://vectors.example.invalid",
        weaviate_collection="Evidence",
        weaviate_token=SecretStr("test-only"),
        local_model_directory="unused",
        generation_model="qwen3:8b",
        generation_digest="a" * 64,
        redis_url=SecretStr("redis://127.0.0.1:6379/15"),
        cache_signing_key=SecretStr("test-only-cache-signing-key-32-bytes"),
    )


def vector_provider(rig: CacheRig) -> WeaviateHybridProvider:
    """Use the actual hybrid composition with controlled numeric and transport boundaries."""
    vectors = SimpleNamespace(
        revision="pinned-model-v1:rrf60:branches100:rerank40:top10",
        check_ready=Mock(),
        rank=lambda question, candidates, limit, **kwargs: candidates[:limit],
    )
    models = SimpleNamespace(
        revision=vectors.revision,
        encode_query=lambda question: [1.0],
        rerank=lambda question, candidates, limit: candidates[:limit],
    )
    return WeaviateHybridProvider(rig.catalog, rig.store, vectors, models)


def test_production_cache_accepts_configured_governed_authority() -> None:
    """The positive deployment contract requires production SQL scope and a signing secret."""
    config = governed_config()
    assert config.mode == "production" and config.catalog_backend == "postgres"
    assert config.governed_catalog_id and config.redis_url.get_secret_value()
    assert len(config.cache_signing_key.get_secret_value().encode()) >= 32


def test_governed_cache_revision_tracks_search_identity_only(rig: CacheRig, monkeypatch) -> None:
    """Actual factory revisions isolate endpoints/models while credential rotation keeps reuse."""
    from creditlens import query_grounding
    from creditlens.runtime import production_cache_revision

    config, provider = governed_config(), vector_provider(rig)
    original = production_cache_revision(config, provider)
    for name, value in (
        ("weaviate_url", "https://new-vectors.example.invalid"),
        ("weaviate_collection", "NewEvidence"),
        ("governed_catalog_id", "another-catalog"),
    ):
        assert (
            production_cache_revision(config.model_copy(update={name: value}), provider) != original
        )
    for name, value in (
        ("weaviate_token", SecretStr("rotated")),
        ("cache_signing_key", SecretStr("another-key")),
        ("generation_digest", "b" * 64),
    ):
        assert (
            production_cache_revision(config.model_copy(update={name: value}), provider) == original
        )
    provider.vectors.revision = "pinned-model-v2:rrf60:branches100:rerank40:top10"
    changed_model = production_cache_revision(config, provider)
    assert changed_model != original
    monkeypatch.setattr(query_grounding, "GROUNDING_VERSION", "new-grounding")
    assert production_cache_revision(config, provider) != changed_model
    cortex = config.model_copy(
        update={"production_search": "cortex", "cortex_url": "https://a.invalid"}
    )
    first_cortex = production_cache_revision(cortex, rig.provider)
    assert first_cortex != original
    assert (
        production_cache_revision(
            cortex.model_copy(update={"cortex_url": "https://b.invalid"}), rig.provider
        )
        != first_cortex
    )


@pytest.mark.parametrize("failure", ["none", "inside-workflow", "cache-constructor"])
def test_governed_factory_wraps_and_closes_cache(rig: CacheRig, monkeypatch, failure: str) -> None:
    """Factory lifecycle closes Redis before search resources on success and both failure points."""
    from creditlens import runtime, sql_catalog

    events = []
    config, provider = governed_config(), vector_provider(rig)
    transport = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    backend = rig.backend
    backend.close = lambda: events.append("redis-closed")

    @contextmanager
    def open_vectors(*args):
        """Track the real provider's owning lifetime around the optional cache wrapper."""
        try:
            yield provider
        finally:
            events.append("vectors-closed")

    def fail_cache(*args, **kwargs):
        """Fail after Redis allocation to verify constructor errors cannot leak its pool."""
        raise ServiceError("cache_configuration_invalid", "Invalid cache configuration", 503)

    monkeypatch.setattr(runtime, "open_generation", lambda settings: nullcontext(None))
    monkeypatch.setattr(runtime, "ProviderClient", lambda **kwargs: transport)
    monkeypatch.setattr(runtime, "open_vector_search", open_vectors)
    monkeypatch.setattr(runtime, "RedisBytes", lambda url: backend)
    monkeypatch.setattr(sql_catalog, "SqlEvidenceCatalog", lambda *args: rig.catalog)
    if failure == "cache-constructor":
        monkeypatch.setattr(runtime, "RetrievalCache", fail_cache)
    outcome = nullcontext() if failure == "none" else pytest.raises(ServiceError)
    with outcome, runtime.open_governed_workflow(config, rig.store) as workflow:
        assert isinstance(workflow.provider, RetrievalCache)
        assert workflow.provider.provider is provider
        first = workflow.provider.search(rig.request, rig.principal)
        hit = workflow.provider.search(rig.request, rig.principal)
        assert first.cache_state == "miss" and hit.cache_state == "hit"
        assert first.chunks == hit.chunks
        workflow.provider.check_ready()
        provider.vectors.check_ready.assert_called_once()
        if failure == "inside-workflow":
            raise ServiceError("query_failed", "Query failed", 503)
    assert events == ["redis-closed", "vectors-closed"]
    assert transport.is_closed
