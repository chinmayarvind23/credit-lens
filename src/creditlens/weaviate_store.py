"""Authenticated vector storage; canonical SQL remains the authority for evidence and scope."""

import json
import math
import re
from collections.abc import Callable
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx
from pydantic import SecretStr

from creditlens.cortex_search import MAX_RESPONSE_BYTES
from creditlens.domain import Chunk
from creditlens.errors import ServiceError
from creditlens.opensearch_provider import validate_search_url

MAX_SCOPE = 1000
DIMENSIONS = 384


def vector_key(chunk: Chunk, namespace: str, revision: str) -> str:
    """Bind embeddings to the entire canonical record, catalog and pinned model revision."""
    value = json.dumps(
        [namespace, revision, chunk.model_dump(mode="json")], sort_keys=True, separators=(",", ":")
    )
    return sha256(value.encode()).hexdigest()


def validate_vector(vector: list[float]) -> None:
    """Reject wrong dimensions, nonfinite values and undefined cosine distances."""
    if (
        len(vector) != DIMENSIONS
        or any(
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            for value in vector
        )
        or not any(vector)
    ):
        raise ValueError("Invalid embedding vector")


class WeaviateStore:
    """The caller owns HTTP lifecycle; queries never mutate or populate the database."""

    def __init__(
        self,
        endpoint: str,
        collection: str,
        client: httpx.Client,
        *,
        namespace: str,
        revision: str,
        token: SecretStr | None = None,
        timeout_seconds: float = 5,
        allow_local_http: bool = False,
    ) -> None:
        """Bind one validated endpoint and immutable model contract to the owned client."""
        validate_search_url(endpoint, "weaviate", allow_local_http)
        if re.fullmatch(r"[A-Z][A-Za-z0-9_]{0,79}", collection) is None:
            raise ValueError("Expected a concrete Weaviate collection")
        if not namespace or not revision or not 0 < timeout_seconds <= 30:
            raise ValueError("Expected catalog, model revision and bounded timeout")
        if endpoint.startswith("http:") and token is not None:
            raise ValueError("Credentialed plaintext endpoints are unsupported")
        self.endpoint = endpoint.rstrip("/")
        self.collection = collection
        self.client = client
        self.namespace = namespace
        self.revision = revision
        self.token = token
        self.timeout_seconds = timeout_seconds

    def request(self, method: str, path: str, body: Any = None) -> Any:
        """Bound responses and attempts; keep backend errors and credentials out of API errors."""
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token.get_secret_value()}"
        try:
            with self.client.stream(
                method,
                self.endpoint + path,
                json=body,
                headers=headers,
                timeout=self.timeout_seconds,
                follow_redirects=False,
            ) as response:
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise ValueError("Encoded response")
                payload = bytearray()
                for block in response.iter_bytes(chunk_size=8192):
                    payload.extend(block)
                    if len(payload) > MAX_RESPONSE_BYTES:
                        raise ValueError("Oversized response")
            return json.loads(payload)
        except (httpx.HTTPError, ValueError, RecursionError) as error:
            raise ServiceError("search_unavailable", "Vector search is unavailable") from error

    def schema(self) -> dict[str, Any]:
        """An explicit model contract prevents accidental reuse with incompatible embeddings."""
        contract = sha256(json.dumps([self.namespace, self.revision]).encode()).hexdigest()
        return {
            "class": self.collection,
            "description": f"creditlens-vector-v1:{contract}",
            "vectorizer": "none",
            "vectorIndexType": "hnsw",
            "vectorIndexConfig": {"distance": "cosine"},
            "properties": [
                {
                    "name": "evidenceKey",
                    "dataType": ["text"],
                    "tokenization": "field",
                    "indexFilterable": True,
                    "indexSearchable": False,
                }
            ],
        }

    def create(self) -> None:
        """Provision only on explicit operator request; existing collections are never replaced."""
        self.request("POST", "/v1/schema", self.schema())
        self.check_ready()

    def check_ready(self) -> None:
        """Check authenticated collection access and the immutable embedding/filter contract."""
        actual = self.request("GET", f"/v1/schema/{self.collection}")
        expected = self.schema()
        if not isinstance(actual, dict) or any(
            actual.get(key) != expected[key]
            for key in ("class", "description", "vectorizer", "vectorIndexType")
        ):
            raise ServiceError("search_index_invalid", "Vector index configuration is unavailable")
        properties = actual.get("properties")
        config = actual.get("vectorIndexConfig")
        if (
            not isinstance(config, dict)
            or config.get("distance") != "cosine"
            or config.get("skip") is True
            or bool(actual.get("vectorConfig"))
            or not isinstance(properties, list)
            or not any(
                isinstance(prop, dict)
                and all(prop.get(k) == v for k, v in expected["properties"][0].items())
                for prop in properties
            )
        ):
            raise ServiceError("search_index_invalid", "Vector index configuration is unavailable")
        # Schema reads alone do not prove query permissions or a loaded searchable shard.
        self._get(["__readiness_probe__"], 1, [1.0] + [0.0] * (DIMENSIONS - 1))

    def _get(
        self,
        keys: list[str],
        limit: int,
        vector: list[float] | None = None,
    ) -> list[str]:
        """Constrain remote results to canonical keys and reject malformed scope responses."""
        near = "" if vector is None else ",nearVector:{vector:" + json.dumps(vector) + "}"
        query = (
            "{Get{"
            + self.collection
            + "(limit:"
            + str(limit)
            + near
            + ',where:{path:["evidenceKey"],operator:ContainsAny,valueText:'
            + json.dumps(keys)
            + "}){evidenceKey}}}"
        )
        body = self.request("POST", "/v1/graphql", {"query": query})
        try:
            if not isinstance(body, dict) or body.get("errors"):
                raise ValueError("GraphQL failure")
            rows = body["data"]["Get"][self.collection]
            if not isinstance(rows, list) or len(rows) > limit:
                raise ValueError("Invalid results")
            identities = [row["evidenceKey"] for row in rows]
            if any(not isinstance(key, str) or key not in keys for key in identities):
                raise ValueError("Out of scope result")
            if len(set(identities)) != len(identities):
                raise ValueError("Duplicate result")
            return identities
        except (KeyError, TypeError, ValueError) as error:
            raise ServiceError(
                "invalid_search_result", "Vector search result is unavailable"
            ) from error

    def keys(self, candidates: tuple[Chunk, ...]) -> list[str]:
        """Generate exact allowlists from already-authorized canonical records."""
        return [vector_key(chunk, self.namespace, self.revision) for chunk in candidates]

    def require_coverage(self, keys: list[str]) -> None:
        """Missing/stale embeddings are an indexing failure, never a plausible empty answer."""
        probe = [1.0] + [0.0] * (DIMENSIONS - 1)
        if set(self._get(keys, len(keys), probe)) != set(keys):
            raise ServiceError("search_index_incomplete", "Vector index requires synchronization")

    def rank(
        self,
        question: str,
        candidates: tuple[Chunk, ...],
        limit: int,
        encode_query: Callable[[str], list[float]],
    ) -> tuple[Chunk, ...]:
        """Use exact scope filters before ANN and hydrate only from the trusted catalog."""
        if not 1 <= limit <= 100 or len(candidates) > MAX_SCOPE:
            raise ServiceError("search_scope_unavailable", "Search scope exceeds the index budget")
        if not candidates:
            return ()
        keys = self.keys(candidates)
        vector = encode_query(question)
        try:
            validate_vector(vector)
        except ValueError as error:
            raise ServiceError("invalid_model_output", "Query embedding is unavailable") from error
        # Enumerate the bounded authorized set in vector order. Object visibility alone cannot
        # prove asynchronous vector indexing is complete; separate reads would introduce a race.
        found = self._get(keys, len(keys), vector)
        if set(found) != set(keys):
            raise ServiceError(
                "search_index_incomplete", "Vector search returned incomplete results"
            )
        canonical = dict(zip(keys, candidates, strict=True))
        return tuple(canonical[key] for key in found[:limit])

    def upsert(self, candidates: tuple[Chunk, ...], vectors: list[list[float]]) -> None:
        """Idempotent immutable objects; verify every batch item and searchable visibility."""
        if not 1 <= len(candidates) <= 100 or len(candidates) != len(vectors):
            raise ValueError("Expected one bounded embedding batch")
        for vector in vectors:
            validate_vector(vector)
        keys = self.keys(candidates)
        objects = [
            {
                "class": self.collection,
                "id": str(uuid5(NAMESPACE_URL, self.collection + ":" + key)),
                "properties": {"evidenceKey": key},
                "vector": vector,
            }
            for key, vector in zip(keys, vectors, strict=True)
        ]
        rows = self.request("POST", "/v1/batch/objects", {"objects": objects})
        if (
            not isinstance(rows, list)
            or len(rows) != len(objects)
            or any(
                not isinstance(row, dict)
                or row.get("id") != obj["id"]
                or not isinstance(row.get("result"), dict)
                or row.get("result", {}).get("status") != "SUCCESS"
                or row.get("result", {}).get("errors")
                for row, obj in zip(rows, objects, strict=True)
            )
        ):
            raise ServiceError("search_index_failed", "Vector indexing failed")
        self.require_coverage(keys)
