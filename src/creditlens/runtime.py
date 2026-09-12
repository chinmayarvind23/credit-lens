"""Own optional cache connections with the application workflow lifecycle."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from creditlens.cache import RedisBytes
from creditlens.corpus import build_demo_pages
from creditlens.local_search import LocalSearchProvider
from creditlens.response_cache import ResponseCache
from creditlens.retrieval import CanonicalCatalog, EvidenceCatalog
from creditlens.retrieval_cache import CanonicalProvider, RetrievalCache
from creditlens.settings import Settings
from creditlens.storage import GrantStore
from creditlens.workflow import QueryWorkflow

if TYPE_CHECKING:
    from creditlens.observability import Telemetry


@contextmanager
def open_workflow(
    config: Settings, store: GrantStore, telemetry: "Telemetry | None" = None
) -> Iterator[QueryWorkflow | None]:
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
    with open_search(config, catalog, store) as provider:
        cache = (
            ResponseCache(
                capacity=config.response_cache_capacity, ttl=config.response_cache_ttl_seconds
            )
            if config.response_cache_enabled
            else None
        )
        yield QueryWorkflow(catalog, store, provider, response_cache=cache, telemetry=telemetry)


@contextmanager
def open_search(
    config: Settings, catalog: CanonicalCatalog, store: GrantStore
) -> Iterator[CanonicalProvider | None]:
    """Load required models at startup and release all optional dependencies on every exit."""
    models = None
    backend = None
    provider: CanonicalProvider | None = None
    version = "local-bm25-k1-1.2-b-0.75-v1"
    try:
        if config.retrieval_mode == "hybrid":
            from creditlens.hybrid_provider import HybridProvider
            from creditlens.neural_search import LocalNeuralRanker
            from creditlens.query_grounding import GROUNDING_VERSION, GroundedProvider
            from creditlens.rerank_provider import RerankProvider

            models = LocalNeuralRanker(Path(config.local_model_directory))
            dense = LocalSearchProvider(
                catalog, store, ranker=models.rank, mode="local-minilm-dense-v1"
            )
            hybrid = HybridProvider(LocalSearchProvider(catalog, store), dense)
            provider = GroundedProvider(
                RerankProvider(hybrid, models.rerank, mode=models.revision), store
            )
            version = f"{GROUNDING_VERSION}:{models.revision}"
        if config.redis_url.get_secret_value():
            backend = RedisBytes(config.redis_url.get_secret_value())
            provider = RetrievalCache(
                provider or LocalSearchProvider(catalog, store),
                store,
                backend,
                config.cache_signing_key.get_secret_value().encode(),
                version,
                ttl=config.cache_ttl_seconds,
            )
        yield provider
    finally:
        try:
            if backend is not None:
                backend.close()
        finally:
            if models is not None:
                models.close()
