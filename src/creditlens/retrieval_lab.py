"""Optional local model experiments preserve the same pre-ranking authorization boundary."""

import importlib
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter
from typing import Any, cast

from creditlens.domain import Chunk

EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L6-v2"
RERANK_REVISION = "233902d25c440f23af6f7d6e94d2946bac0bee0a"


def model_snapshot(model: str, revision: str, directory: Path) -> str:
    """Use real local files because this Windows environment cannot read Hub cache symlinks."""
    hub = importlib.import_module("huggingface_hub")
    return cast(
        str,
        hub.snapshot_download(
            repo_id=model,
            revision=revision,
            local_dir=str(directory),
            allow_patterns=["*.json", "*.txt", "*.safetensors"],
            max_workers=4,
        ),
    )


class DenseLab:
    """Offline encoding can cover the corpus; query similarity sees authorized rows only."""

    def __init__(self, chunks: Sequence[Chunk], cache: Path) -> None:
        """Pin safe model weights and record actual preprocessing time rather than hiding it."""
        transformers = importlib.import_module("sentence_transformers")
        self.np = importlib.import_module("numpy")
        torch = importlib.import_module("torch")
        torch.set_num_threads(4)
        load_started = perf_counter()
        snapshot = model_snapshot(EMBED_MODEL, EMBED_REVISION, cache / "embedding")
        self.embedding_snapshot_seconds = perf_counter() - load_started
        model_started = perf_counter()
        self.model = transformers.SentenceTransformer(
            snapshot,
            device="cpu",
            trust_remote_code=False,
            model_kwargs={"use_safetensors": True},
        )
        self.embedding_load_seconds = perf_counter() - model_started
        self.reranker: Any = None
        self.cache = cache
        self.indices = {chunk.chunk_id: index for index, chunk in enumerate(chunks)}
        started = perf_counter()
        self.vectors = self.model.encode_document(
            [chunk.text for chunk in chunks],
            batch_size=64,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        ).astype("float32")
        self.indexing_seconds = perf_counter() - started
        self.query_vectors: dict[str, Any] = {}

    def encode_query(self, question: str) -> Any:
        """Reuse a local query vector only within this experiment and disclose warm-query timing."""
        if question not in self.query_vectors:
            self.query_vectors[question] = self.model.encode_query(
                [question],
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            ).astype("float32")
        return self.query_vectors[question]

    def rank(
        self, question: str, candidates: tuple[Chunk, ...], limit: int = 100
    ) -> tuple[Chunk, ...]:
        """Slice to current allowed identities before NumPy computes any query similarity score."""
        if not candidates:
            return ()
        indices = [self.indices[chunk.chunk_id] for chunk in candidates]
        scores = self.vectors[indices] @ self.encode_query(question)[0]
        ordered = sorted(
            range(len(candidates)), key=lambda i: (-float(scores[i]), candidates[i].chunk_id)
        )
        return tuple(candidates[index] for index in ordered[:limit])

    def rerank(
        self, question: str, candidates: tuple[Chunk, ...], limit: int = 100
    ) -> tuple[Chunk, ...]:
        """The cross-encoder receives only authorized candidate text, with pinned safetensors."""
        if not candidates:
            return ()
        if self.reranker is None:
            transformers = importlib.import_module("sentence_transformers")
            snapshot = model_snapshot(RERANK_MODEL, RERANK_REVISION, self.cache / "reranker")
            self.reranker = transformers.CrossEncoder(
                snapshot,
                device="cpu",
                trust_remote_code=False,
                model_kwargs={"use_safetensors": True},
            )
        scores = self.reranker.predict(
            [(question, chunk.text) for chunk in candidates], batch_size=32, show_progress_bar=False
        )
        ordered = sorted(
            range(len(candidates)), key=lambda i: (-float(scores[i]), candidates[i].chunk_id)
        )
        return tuple(candidates[index] for index in ordered[:limit])

    def faiss_agreement(
        self, question: str, candidates: tuple[Chunk, ...], k: int = 10
    ) -> dict[str, float | None]:
        """Compare scoped HNSW to exact NumPy and report scope-index construction overhead."""
        if not candidates:
            return {"agreement_at_10": None, "scope_index_ms": 0.0, "search_ms": 0.0}
        faiss = importlib.import_module("faiss")
        faiss.omp_set_num_threads(1)
        vectors = self.vectors[[self.indices[chunk.chunk_id] for chunk in candidates]]
        started = perf_counter()
        index = faiss.IndexHNSWFlat(vectors.shape[1], 16, faiss.METRIC_INNER_PRODUCT)
        index.hnsw.efConstruction = 80
        index.hnsw.efSearch = 64
        index.add(vectors)
        indexed = perf_counter()
        query = self.encode_query(question)
        _, identifiers = index.search(query, min(k, len(candidates)))
        searched = perf_counter()
        approximate = {
            candidates[i].chunk_id for i in cast(list[int], identifiers[0].tolist()) if i >= 0
        }
        exact = {chunk.chunk_id for chunk in self.rank(question, candidates, limit=k)}
        return {
            "agreement_at_10": len(approximate & exact) / len(exact),
            "scope_index_ms": (indexed - started) * 1000,
            "search_ms": (searched - indexed) * 1000,
        }
