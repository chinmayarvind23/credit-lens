"""Offline CPU ranking with bounded retained vectors and no cross-request question cache."""

import importlib
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Any

from creditlens.domain import Chunk
from creditlens.errors import ServiceError
from creditlens.model_bundle import verify_bundle


def input_budget(question: str, candidates: tuple[Chunk, ...], limit: int, maximum: int) -> None:
    """Bound text admission before tokenization; token truncation is a separate model contract."""
    if not 1 <= limit <= 100 or len(question) > 2000 or len(candidates) > maximum:
        raise ServiceError("model_input_limit", "Evidence exceeds the model input budget", 422)
    sizes = [len(chunk.text.encode("utf-8")) for chunk in candidates]
    if any(size > 16000 for size in sizes) or sum(sizes) > 1_000_000:
        raise ServiceError("model_input_limit", "Evidence exceeds the model input budget", 422)


class LocalNeuralRanker:
    """Load pinned weights once; score only the already-authorized candidate sequence."""

    def __init__(self, directory: Path, *, cache_entries: int = 4096) -> None:
        """Verify before optional ML imports; never request cloud inference or downloads."""
        if not 1 <= cache_entries <= 4096:
            raise ValueError("Embedding retention must be between 1 and 4096 entries")
        embedding, reranker, revision = verify_bundle(directory)
        self.revision = f"local-minilm-v1:{revision}:rrf60:branches100:rerank40:top10"
        self.cache_entries = cache_entries
        self._vectors: OrderedDict[tuple[str, str, str], Any] = OrderedDict()
        self._lock = Lock()
        self._closed = False
        try:
            transformers = importlib.import_module("sentence_transformers")
            torch = importlib.import_module("torch")
            self.np = importlib.import_module("numpy")
            torch.set_num_threads(4)
            self.embedding = transformers.SentenceTransformer(
                str(embedding),
                device="cpu",
                local_files_only=True,
                trust_remote_code=False,
                token=False,
                model_kwargs={"use_safetensors": True},
            )
            self.reranker = transformers.CrossEncoder(
                str(reranker),
                device="cpu",
                local_files_only=True,
                trust_remote_code=False,
                token=False,
                max_length=512,
                model_kwargs={"use_safetensors": True},
            )
            self.embedding.max_seq_length = 256
        except (ImportError, OSError, ValueError, RuntimeError) as exc:
            raise ServiceError("model_unavailable", "Local ranking models are unavailable") from exc

    @contextmanager
    def _inference(self) -> Iterator[None]:
        """Allow one active inference; reject overload instead of queuing CPU work."""
        if not self._lock.acquire(blocking=False):
            raise ServiceError("model_busy", "Local ranking is busy; retry the request", 503)
        try:
            if self._closed:
                raise ServiceError("model_unavailable", "Local ranking models are unavailable")
            yield
        except (OSError, ValueError, RuntimeError) as exc:
            raise ServiceError("model_unavailable", "Local ranking models are unavailable") from exc
        finally:
            self._lock.release()

    def _matrix(self, candidates: tuple[Chunk, ...]) -> Any:
        """Bound LRU vectors by identity and text hash; only passed candidates are scored."""
        keys = [(c.tenant_id, c.chunk_id, sha256(c.text.encode()).hexdigest()) for c in candidates]
        missing = [i for i, key in enumerate(keys) if key not in self._vectors]
        fresh: dict[int, Any] = {}
        if missing:
            encoded = self.embedding.encode_document(
                [candidates[i].text for i in missing],
                batch_size=32,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            ).astype("float32")
            self._shape(encoded, (len(missing), 384))
            fresh = dict(zip(missing, encoded, strict=True))
        rows = []
        for index, key in enumerate(keys):
            vector = fresh[index] if index in fresh else self._vectors[key]
            rows.append(vector)
        for key, vector in zip(keys, rows, strict=True):
            self._vectors[key] = vector.copy()
            self._vectors.move_to_end(key)
            if len(self._vectors) > self.cache_entries:
                self._vectors.popitem(last=False)
        return self.np.stack(rows)

    def _shape(self, array: Any, shape: tuple[int, ...]) -> None:
        """Malformed or nonfinite model output cannot define a plausible-looking ranking."""
        if array.shape != shape or not self.np.isfinite(array).all():
            raise ServiceError("invalid_model_output", "Local ranking output is unavailable")

    def _order(self, scores: Any, candidates: tuple[Chunk, ...], limit: int) -> tuple[Chunk, ...]:
        """Keep deterministic chunk-ID ties identical to the measured offline experiment."""
        self._shape(scores, (len(candidates),))
        ordered = sorted(
            range(len(candidates)), key=lambda i: (-float(scores[i]), candidates[i].chunk_id)
        )
        return tuple(candidates[index] for index in ordered[:limit])

    def rank(
        self, question: str, candidates: tuple[Chunk, ...], limit: int = 100
    ) -> tuple[Chunk, ...]:
        """Compute cosine similarity inside current scope without retaining the query vector."""
        input_budget(question, candidates, limit, 512)
        if not candidates:
            return ()
        with self._inference():
            matrix = self._matrix(candidates)
            query = self.embedding.encode_query(
                [question],
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            ).astype("float32")
            self._shape(query, (1, 384))
            return self._order(matrix @ query[0], candidates, limit)

    def rerank(
        self, question: str, candidates: tuple[Chunk, ...], limit: int = 10
    ) -> tuple[Chunk, ...]:
        """Score at most 40 authorized pairs with explicit 512-token truncation and stable ties."""
        input_budget(question, candidates, limit, 40)
        if not candidates:
            return ()
        with self._inference():
            scores = self.np.asarray(
                self.reranker.predict(
                    [(question, chunk.text) for chunk in candidates],
                    batch_size=32,
                    show_progress_bar=False,
                )
            )
            return self._order(scores, candidates, limit)

    def close(self) -> None:
        """Application shutdown releases retained evidence vectors and model references."""
        with self._lock:
            self._vectors.clear()
            self.embedding = None
            self.reranker = None
            self._closed = True
