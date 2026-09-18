"""Own optional cache connections with the application workflow lifecycle."""

import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from hashlib import sha256
from pathlib import Path
from ssl import create_default_context
from typing import TYPE_CHECKING

from httpx import Client as ProviderClient

from creditlens.cache import RedisBytes
from creditlens.corpus import build_demo_pages
from creditlens.generation_contract import AnswerGenerator
from creditlens.generation_transport import GenerationClient
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
        with open_governed_workflow(config, store, telemetry) as workflow:
            yield workflow
        return
    catalog: CanonicalCatalog
    if config.catalog_backend == "postgres":
        from creditlens.sql_catalog import initialize_catalog

        shared = initialize_catalog(store.engine, config.demo_catalog_id)
        shared.publish(build_demo_pages())
        catalog = shared
    else:
        catalog = EvidenceCatalog(build_demo_pages())
    with (
        open_search(config, catalog, store, telemetry) as provider,
        open_generation(config) as generator,
    ):
        cache = (
            ResponseCache(
                capacity=config.response_cache_capacity, ttl=config.response_cache_ttl_seconds
            )
            if config.response_cache_enabled
            else None
        )
        yield QueryWorkflow(
            catalog, store, provider, response_cache=cache, telemetry=telemetry, generator=generator
        )


@contextmanager
def open_search(
    config: Settings,
    catalog: CanonicalCatalog,
    store: GrantStore,
    telemetry: "Telemetry | None" = None,
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

            models = LocalNeuralRanker(Path(config.local_model_directory), telemetry=telemetry)
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


@contextmanager
def open_governed_workflow(
    config: Settings, store: GrantStore, telemetry: "Telemetry | None" = None
) -> Iterator[QueryWorkflow | None]:
    """Read an existing governed catalog; never seed demo data or silently fall back on search."""
    if not config.governed_catalog_id:
        yield None
        return
    from creditlens.cortex_search import CortexSearchProvider
    from creditlens.sql_catalog import SqlEvidenceCatalog

    catalog = SqlEvidenceCatalog(store.engine, config.governed_catalog_id)
    with ExitStack() as stack:
        generator = stack.enter_context(open_generation(config))
        client = stack.enter_context(
            ProviderClient(
                timeout=config.request_timeout_seconds,
                trust_env=False,
                follow_redirects=False,
                verify=create_default_context(cafile=config.weaviate_ca_file or None),
            )
        )
        provider: CanonicalProvider
        if config.production_search == "weaviate":
            provider = stack.enter_context(
                open_vector_search(config, catalog, store, client, telemetry)
            )
        else:
            provider = CortexSearchProvider(
                config.cortex_url,
                config.cortex_token,
                client,
                catalog,
                store,
                timeout_seconds=config.request_timeout_seconds,
            )
        if config.redis_url.get_secret_value():
            backend = RedisBytes(config.redis_url.get_secret_value())
            stack.callback(backend.close)
            provider = RetrievalCache(
                provider,
                store,
                backend,
                config.cache_signing_key.get_secret_value().encode(),
                production_cache_revision(config, provider),
                ttl=config.cache_ttl_seconds,
            )
        cache = (
            ResponseCache(
                capacity=config.response_cache_capacity, ttl=config.response_cache_ttl_seconds
            )
            if config.response_cache_enabled
            else None
        )
        yield QueryWorkflow(
            catalog, store, provider, response_cache=cache, telemetry=telemetry, generator=generator
        )


def production_cache_revision(config: Settings, provider: CanonicalProvider) -> str:
    """Bind cached rankings to immutable retrieval configuration without retaining credentials."""
    from creditlens.query_grounding import GROUNDING_VERSION
    from creditlens.weaviate_provider import WeaviateHybridProvider

    identity = ["governed-retrieval-v1", config.production_search, config.governed_catalog_id]
    if isinstance(provider, WeaviateHybridProvider):
        identity.extend(
            [
                config.weaviate_url,
                config.weaviate_collection,
                GROUNDING_VERSION,
                provider.vectors.revision,
            ]
        )
    else:
        identity.extend(["cortex-canonical-v1", config.cortex_url])
    return sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


@contextmanager
def open_generation(config: Settings) -> Iterator[AnswerGenerator | None]:
    """Verify explicitly configured generation at startup and close its separate HTTP lifecycle."""
    if not config.generation_model:
        yield None
        return
    from creditlens.ollama_generation import OllamaGenerator

    with GenerationClient(trust_env=False, follow_redirects=False) as client:
        generator = OllamaGenerator(
            config.generation_url,
            config.generation_model,
            config.generation_digest,
            client,
            token=config.generation_token,
            timeout_seconds=config.generation_timeout_seconds,
        )
        generator.check_ready()
        yield generator


@contextmanager
def open_vector_search(
    config: Settings,
    catalog: CanonicalCatalog,
    store: GrantStore,
    client: ProviderClient,
    telemetry: "Telemetry | None" = None,
) -> Iterator[CanonicalProvider]:
    """Require the vector branch while preserving lexical signals and existing reranking."""
    from creditlens.neural_search import LocalNeuralRanker
    from creditlens.weaviate_provider import WeaviateHybridProvider
    from creditlens.weaviate_store import WeaviateStore

    models = LocalNeuralRanker(Path(config.local_model_directory), telemetry=telemetry)
    try:
        vectors = WeaviateStore(
            config.weaviate_url,
            config.weaviate_collection,
            client,
            namespace=config.governed_catalog_id,
            revision=models.revision,
            token=config.weaviate_token,
            timeout_seconds=config.request_timeout_seconds,
        )
        vectors.check_ready()
        yield WeaviateHybridProvider(catalog, store, vectors, models)
    finally:
        models.close()
