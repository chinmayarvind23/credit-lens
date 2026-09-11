"""Explicit local Redis integration; invoked only with an isolated synthetic Redis URL."""

import os
import time
from datetime import date
from secrets import token_bytes
from unittest.mock import Mock

from creditlens.cache import RedisBytes
from creditlens.citations import cite
from creditlens.corpus import build_demo_pages
from creditlens.domain import QueryRequest
from creditlens.local_search import LocalSearchProvider
from creditlens.retrieval import EvidenceCatalog
from creditlens.retrieval_cache import RetrievalCache
from creditlens.storage import GrantStore, open_database


def test_real_redis_cache_lifecycle() -> None:
    """Real Redis hit/expiry/tamper/revocation paths use the actual canonical BM25 provider."""
    url = os.environ["CREDITLENS_TEST_REDIS_URL"]
    backend = RedisBytes(url)
    engine = open_database("sqlite:///:memory:")
    store = GrantStore(engine)
    store.seed_demo()
    principal = store.resolve("synthetic-demo")
    catalog = EvidenceCatalog(build_demo_pages())
    provider = LocalSearchProvider(catalog, store)
    observed_search = Mock(wraps=provider.search)
    provider.search = observed_search
    cache = RetrievalCache(provider, store, backend, token_bytes(32), "local-bm25-v1", ttl=1)
    request = QueryRequest(
        borrower_id="borrower-001", question="debt service coverage", effective_at=date(2026, 9, 11)
    )
    try:
        first = cache.search(request, principal)
        hit = cache.search(request, principal)
        assert first.cache_state == "miss" and hit.cache_state == "hit"
        assert observed_search.call_count == 1 and hit.chunks == first.chunks
        assert cache.citation(hit, cite(hit.chunks[0])) == hit.chunks[0]
        time.sleep(1.1)
        expired = cache.search(request, principal)
        assert expired.cache_state == "miss" and observed_search.call_count == 2
        key = cache.key(request, principal, expired.catalog_revision, 10)
        backend.put(key, b"tampered", 60)
        repaired = cache.search(request, principal)
        assert repaired.cache_state == "invalid" and observed_search.call_count == 3
        catalog.revoke(repaired.chunks[0].chunk_id)
        revoked = cache.search(request, principal)
        assert repaired.chunks[0] not in revoked.chunks and observed_search.call_count == 4
    finally:
        backend.close()
        engine.dispose()
