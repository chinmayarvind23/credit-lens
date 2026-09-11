"""Explicit local Redis integration; invoked only with an isolated synthetic Redis URL."""

import os
import time
from datetime import date
from secrets import token_bytes
from unittest.mock import Mock

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from creditlens.api import create_app
from creditlens.cache import RedisBytes
from creditlens.citations import cite
from creditlens.corpus import build_demo_pages
from creditlens.domain import QueryRequest
from creditlens.local_search import LocalSearchProvider
from creditlens.retrieval import EvidenceCatalog
from creditlens.retrieval_cache import RetrievalCache
from creditlens.settings import Settings
from creditlens.storage import GrantStore, audit_events, open_database


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


def test_real_redis_http_hit_recomputes_audited_packet() -> None:
    """The configured ASGI lifecycle reaches real Redis and rejects a warmed revoked source."""
    settings = Settings(
        database_url="sqlite:///:memory:",
        redis_url=SecretStr(os.environ["CREDITLENS_TEST_REDIS_URL"]),
        cache_signing_key=SecretStr(token_bytes(32).hex()),
        cache_ttl_seconds=5,
    )
    request = {
        "borrower_id": "borrower-001",
        "question": "Prepare the DSCR underwriting packet",
        "effective_at": "2026-06-01",
    }
    with TestClient(create_app(settings)) as client:
        first = client.post("/api/v1/query", json=request)
        hit = client.post("/api/v1/query", json=request)
        assert first.status_code == hit.status_code == 200
        before, after = first.json(), hit.json()
        assert not before["cache_hit"] and after["cache_hit"]
        assert before["calculated_metrics"] == after["calculated_metrics"]
        assert before["evidence"] == after["evidence"]
        assert before["request_id"] != after["request_id"]
        workflow = client.app.state.workflow
        source = next(c for c in after["evidence"] if c["section"] == "financial_summary")
        workflow.catalog.revoke(source["chunk_id"])
        revoked = client.post("/api/v1/query", json=request).json()
        assert not revoked["cache_hit"] and revoked["abstained"]
        assert all(c["chunk_id"] != source["chunk_id"] for c in revoked["evidence"])
        with workflow.store.engine.connect() as connection:
            rows = connection.execute(select(audit_events)).mappings().all()
        assert len(rows) == 3
        assert rows[1]["event"]["protected_packet"]["cache_hit"] is True
