"""Opt-in real local database controls, separate from mocked HTTP contract tests."""

import os
from uuid import uuid4

import httpx
import pytest

from infra.weaviate.lab import create, populate, search, tune


@pytest.mark.skipif(
    os.environ.get("CREDITLENS_TEST_WEAVIATE") != "1", reason="local Weaviate opt-in"
)
def test_live_weaviate_prefilter() -> None:
    """A higher-scoring forbidden vector cannot appear at either tested exploration depth."""
    np = pytest.importorskip("numpy")
    name = "CreditlensControl" + uuid4().hex
    with httpx.Client(base_url="http://127.0.0.1:18081", timeout=30, trust_env=False) as client:
        create(client, name, 8)
        try:
            populate(
                client,
                name,
                ["allowed-a", "allowed-b", "forbidden"],
                np.array([[0.8, 0.6], [0.0, 1.0], [1.0, 0.0]], dtype="float32"),
            )
            for ef in (16, 64):
                schema = tune(client, name, ef)
                assert schema["vectorIndexType"] == "hnsw"
                assert schema["vectorIndexConfig"]["maxConnections"] == 8
                assert search(client, name, [1.0, 0.0], ["allowed-a", "allowed-b"]) == [
                    "allowed-a",
                    "allowed-b",
                ]
                assert search(client, name, [1.0, 0.0], ["allowed-b"]) == ["allowed-b"]
            assert search(client, name, [1.0, 0.0], []) == []
        finally:
            client.delete(f"/v1/schema/{name}").raise_for_status()
