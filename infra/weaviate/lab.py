"""Bounded local synthetic Weaviate experiment using canonical allowlists and supplied vectors."""

import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx


def create(client: httpx.Client, name: str, connections: int) -> dict[str, Any]:
    """Disable flat fallback and compression so the experiment actually exercises HNSW."""
    schema = {
        "class": name,
        "vectorizer": "none",
        "vectorIndexType": "hnsw",
        "vectorIndexConfig": {
            "distance": "cosine",
            "maxConnections": connections,
            "efConstruction": 80,
            "ef": 16,
            "flatSearchCutoff": 0,
            "rq": {"enabled": False},
            "pq": {"enabled": False},
            "bq": {"enabled": False},
        },
        "properties": [{"name": "chunkId", "dataType": ["text"], "tokenization": "field"}],
    }
    response = client.post("/v1/schema", json=schema)
    response.raise_for_status()
    return schema


def populate(client: httpx.Client, name: str, ids: list[str], vectors: Any) -> None:
    """Validate every batch result; a successful HTTP status alone does not prove indexing."""
    for offset in range(0, len(ids), 100):
        objects = [
            {
                "class": name,
                "id": str(uuid5(NAMESPACE_URL, name + identity)),
                "properties": {"chunkId": identity},
                "vector": vectors[i].tolist(),
            }
            for i, identity in enumerate(ids[offset : offset + 100], start=offset)
        ]
        response = client.post("/v1/batch/objects", json={"objects": objects})
        response.raise_for_status()
        rows = response.json()
        if len(rows) != len(objects) or any(r.get("result", {}).get("errors") for r in rows):
            raise RuntimeError("Weaviate batch contains failed objects")


def tune(client: httpx.Client, name: str, ef: int) -> dict[str, Any]:
    """Read back server configuration after changing the mutable search exploration bound."""
    response = client.get(f"/v1/schema/{name}")
    response.raise_for_status()
    schema = response.json()
    schema["vectorIndexConfig"]["ef"] = ef
    response = client.put(f"/v1/schema/{name}", json=schema)
    response.raise_for_status()
    response = client.get(f"/v1/schema/{name}")
    response.raise_for_status()
    actual = response.json()
    config = actual["vectorIndexConfig"]
    if config["ef"] != ef or config["flatSearchCutoff"] != 0:
        raise RuntimeError("Requested HNSW settings were not applied")
    return actual


def search(
    client: httpx.Client,
    name: str,
    vector: list[float],
    allowed: list[str],
    details: dict[str, Any] | None = None,
) -> list[str]:
    """Send exact canonical IDs as a prefilter, then reject any out-of-scope returned identity."""
    if not allowed:
        return []
    query = (
        "{Get{"
        + name
        + "(limit:"
        + str(min(10, len(allowed)))
        + ",nearVector:{vector:"
        + json.dumps(vector)
        + "},"
        'where:{path:["chunkId"],operator:ContainsAny,valueText:'
        + json.dumps(allowed)
        + "}){chunkId _additional{distance}}}}"
    )
    response = client.post("/v1/graphql", json={"query": query})
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError("Weaviate GraphQL search failed")
    result = [row["chunkId"] for row in body["data"]["Get"][name]]
    if not set(result).issubset(allowed):
        raise RuntimeError("Weaviate returned unauthorized identities")
    # Repeated ANN IDs earn no extra relevance credit; retain the raw server behavior for review.
    if details is not None:
        details.update(raw_chunk_ids=result, duplicate_count=len(result) - len(set(result)))
    return list(dict.fromkeys(result))
