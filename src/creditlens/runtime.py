"""Own optional cache connections with the application workflow lifecycle."""

from collections.abc import Iterator
from contextlib import contextmanager

from creditlens.cache import RedisBytes
from creditlens.corpus import build_demo_pages
from creditlens.local_search import LocalSearchProvider
from creditlens.retrieval import CanonicalCatalog, EvidenceCatalog
from creditlens.retrieval_cache import RetrievalCache
from creditlens.settings import Settings
from creditlens.storage import GrantStore
from creditlens.workflow import QueryWorkflow


@contextmanager
def open_workflow(config: Settings, store: GrantStore) -> Iterator[QueryWorkflow | None]:
    """Close Redis on every exit; a remote outage stays an observable optional-cache miss."""
    if config.mode != "demo":
        yield None
        return
    catalog: CanonicalCatalog
    if config.catalog_backend == "postgres":
        from creditlens.sql_catalog import initialize_catalog

        shared = initialize_catalog(store.engine, config.demo_catalog_id)
        shared.publish(build_demo_pages())
        catalog = shared
    else:
        catalog = EvidenceCatalog(build_demo_pages())
    if not config.redis_url.get_secret_value():
        yield QueryWorkflow(catalog, store)
        return
    backend = RedisBytes(config.redis_url.get_secret_value())
    try:
        provider = RetrievalCache(
            LocalSearchProvider(catalog, store),
            store,
            backend,
            config.cache_signing_key.get_secret_value().encode(),
            "local-bm25-k1-1.2-b-0.75-v1",
            ttl=config.cache_ttl_seconds,
        )
        yield QueryWorkflow(catalog, store, provider)
    finally:
        backend.close()
