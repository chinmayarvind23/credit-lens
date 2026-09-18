"""Compose persistent vector retrieval with the existing canonical authorization boundary."""

from functools import partial

from creditlens.hybrid_provider import HybridProvider
from creditlens.local_search import LocalSearchProvider
from creditlens.neural_search import LocalNeuralRanker
from creditlens.query_grounding import GroundedProvider
from creditlens.rerank_provider import RerankProvider
from creditlens.retrieval import CanonicalCatalog
from creditlens.storage import GrantStore
from creditlens.weaviate_store import WeaviateStore


class WeaviateProvider(LocalSearchProvider):
    """Reuse grant, revision and citation checks before and after remote ANN ranking."""

    def __init__(
        self,
        catalog: CanonicalCatalog,
        store: GrantStore,
        vectors: WeaviateStore,
        models: LocalNeuralRanker,
    ) -> None:
        """Inject remote ranking while retaining canonical authorization and hydration."""
        super().__init__(
            catalog,
            store,
            ranker=partial(vectors.rank, encode_query=models.encode_query),
            mode="weaviate-hnsw-v1",
        )
        self.vectors = vectors


class WeaviateHybridProvider(GroundedProvider):
    """Expose dependency readiness without coupling the HTTP layer to wrapper internals."""

    def __init__(
        self,
        catalog: CanonicalCatalog,
        store: GrantStore,
        vectors: WeaviateStore,
        models: LocalNeuralRanker,
    ) -> None:
        """Keep fusion, reranking and grounding identical to the governed local path."""
        dense = WeaviateProvider(catalog, store, vectors, models)
        hybrid = HybridProvider(LocalSearchProvider(catalog, store), dense)
        super().__init__(RerankProvider(hybrid, models.rerank, mode=models.revision), store)
        self.vectors = vectors

    def check_ready(self) -> None:
        """Readiness verifies the required remote branch, including query access."""
        self.vectors.check_ready()
