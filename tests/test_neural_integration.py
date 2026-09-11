"""Optional actual-model HTTP checks require an operator's verified offline model directory."""

import json
import os
import socket
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

import httpx
import pytest
from sqlalchemy import select

from creditlens.domain import Packet
from creditlens.evaluation import source_hashes
from creditlens.settings import Settings
from creditlens.storage import audit_events, grants
from scripts.benchmark_cache_http import serve


def test_real_local_models_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise real models, TCP HTTP and revocation while denying external sockets."""
    directory = os.environ.get("CREDITLENS_TEST_MODEL_DIRECTORY")
    if not directory:
        pytest.skip("Actual neural integration requires CREDITLENS_TEST_MODEL_DIRECTORY")
    original = socket.socket.connect
    external = []

    def connect(self, address):
        """Permit only this loopback HTTP test; any attempted remote model call fails the test."""
        if address[0] != "127.0.0.1":
            external.append(address[0])
            raise AssertionError("Offline model attempted an external connection")
        return original(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    before = source_hashes(Path.cwd())
    records = []
    with TemporaryDirectory() as temporary:
        config = Settings(
            database_url=f"sqlite:///{Path(temporary).as_posix()}/audit.db",
            retrieval_mode="hybrid",
            local_model_directory=directory,
        )
        with (
            serve(config, startup_timeout=120) as (url, app),
            httpx.Client(base_url=url, timeout=30, trust_env=False) as client,
        ):
            assert client.get("/ready").status_code == 200
            models = app.state.workflow.provider.ranker.__self__
            for number, expected in (
                (1, "MEETS_POLICY"),
                (2, "EXCEPTION_REQUIRED"),
                (3, "INSUFFICIENT_EVIDENCE"),
                (4, "MATERIAL_CONFLICT"),
                (5, "MEETS_POLICY"),
            ):
                body = {
                    "borrower_id": f"borrower-{number:03d}",
                    "question": "Calculate debt service coverage and identify policy exceptions",
                    "effective_at": "2026-09-11",
                }
                started = perf_counter()
                response = client.post("/api/v1/query", json=body)
                elapsed = (perf_counter() - started) * 1000
                assert response.status_code == 200, response.text
                packet = Packet.model_validate(response.json())
                assert packet.policy_disposition == expected
                assert all(c.borrower_id in (None, body["borrower_id"]) for c in packet.evidence)
                source = packet.evidence[0]
                inspected = client.get(
                    f"/api/v1/evidence/{source.chunk_id}",
                    params={
                        "borrower_id": body["borrower_id"],
                        "effective_at": body["effective_at"],
                    },
                )
                assert inspected.status_code == 200
                assert inspected.json()["text"] == source.text
                records.append(
                    {"http_round_trip_ms": elapsed, "packet": packet.model_dump(mode="json")}
                )
            body["borrower_id"] = "borrower-999"
            assert client.post("/api/v1/query", json=body).status_code == 403
            body.update(
                borrower_id="borrower-001", question="When does the nearby planetarium open?"
            )
            assert client.post("/api/v1/query", json=body).json()["abstained"] is True
            models._lock.acquire()
            try:
                busy = client.post("/api/v1/query", json=body)
                assert busy.status_code == 503 and busy.json()["error"]["code"] == "model_busy"
            finally:
                models._lock.release()
            with app.state.store.engine.begin() as connection:
                events = connection.execute(select(audit_events.c.event)).scalars().all()
                assert len(events) == 6
                assert all("reranked:local-minilm-v1:" in e["search_provider_mode"] for e in events)
                connection.execute(grants.update().values(enabled=False, revision=2))
            assert client.post("/api/v1/query", json=body).status_code == 403
        assert models._closed
    assert external == []
    assert before == source_hashes(Path.cwd())
    output = os.environ.get("CREDITLENS_TEST_MODEL_EVIDENCE")
    if output:
        Path(output).write_text(
            json.dumps(
                {
                    "status": "passed",
                    "source_hashes": before,
                    "model_revision": models.revision,
                    "external_connections": external,
                    "records": records,
                    "scope": (
                        "five real TCP HTTP financial scenarios plus source, denial, abstention, "
                        "overload and revocation checks; no population latency claim"
                    ),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
