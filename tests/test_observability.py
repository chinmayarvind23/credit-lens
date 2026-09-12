"""Actual OTel SDK export and Prometheus exposition must remain private and request-correlated."""

import json
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

from creditlens.api import create_app
from creditlens.observability import Telemetry
from creditlens.settings import Settings
from creditlens.storage import grants


@pytest.fixture
def trace_directory() -> Iterator[Path]:
    """Own a temporary directory without pytest current symlinks rejected by this Windows host."""
    with TemporaryDirectory(prefix="creditlens-trace-tests-") as directory:
        yield Path(directory)


def test_http_traces_metrics_cache_and_privacy(trace_directory: Path) -> None:
    """Real SDK spans join the HTTP root across the FastAPI thread boundary and omit input text."""
    output = trace_directory / "traces.jsonl"
    config = Settings(
        database_url="sqlite:///:memory:",
        response_cache_enabled=True,
        telemetry_enabled=True,
        trace_file=str(output),
    )
    app = create_app(config)
    with TestClient(app) as client:
        body = {
            "borrower_id": "borrower-001",
            "question": "Calculate debt service coverage SECRET-MARKER",
            "effective_at": "2026-06-01",
        }
        assert client.post("/api/v1/query", json=body).status_code == 200
        assert client.post("/api/v1/query", json=body).json()["cache_hit"]
        assert (
            client.post("/api/v1/query", json={**body, "borrower_id": "borrower-999"}).status_code
            == 403
        )
        assert client.get("/api/v1/metrics").status_code == 403
        with app.state.store.engine.begin() as connection:
            connection.execute(grants.update().values(role="admin", revision=2))
        result = client.get("/api/v1/metrics")
        assert result.status_code == 200
        metrics = result.text
        assert 'route="/api/v1/query",status="2xx"} 2.0' in metrics
        assert 'cache="hit"' in metrics
        assert "creditlens_request_duration_seconds_bucket" in metrics
        assert 'creditlens_stage_duration_seconds_count{stage="audit.persist"} 2.0' in metrics
        assert 'creditlens_acl_denials_total{status="403"} 2.0' in metrics
        assert (
            client.get(
                "/api/v1/evidence/private-document",
                params={"borrower_id": "borrower-001", "effective_at": "2026-06-01"},
            ).status_code
            == 404
        )
    text = output.read_text(encoding="utf-8")
    spans = [json.loads(line) for line in text.splitlines()]
    roots = {s["trace_id"]: s["span_id"] for s in spans if s["name"] == "http.request"}
    stages = [s for s in spans if s["name"] == "audit.persist"]
    assert len(stages) == 2
    assert all(s["parent_span_id"] == roots[s["trace_id"]] for s in stages)
    assert any(s["name"] == "cache.response.hit" for s in spans)
    # Numeric substrings can occur in legitimate timestamps; check the private field name instead.
    for secret in (
        "SECRET-MARKER",
        "borrower-001",
        "synthetic-demo",
        "private-document",
        "operating_cash_flow",
    ):
        assert secret not in text + metrics


def test_span_errors_do_not_export_exception_messages(trace_directory: Path) -> None:
    """Failed stage status is useful; confidential exception strings must not be emitted."""
    output = trace_directory / "error.jsonl"
    telemetry = Telemetry(str(output))
    with pytest.raises(RuntimeError), telemetry.span("controlled.failure"):
        raise RuntimeError("private-provider-body")
    telemetry.close()
    span = json.loads(output.read_text())
    assert span["status"] == "ERROR"
    assert span["name"] == "other"
    metrics = telemetry.render().decode()
    assert 'creditlens_stage_errors_total{stage="other"} 1.0' in metrics
    assert 'creditlens_stage_duration_seconds_count{stage="other"} 1.0' in metrics
    assert "controlled.failure" not in metrics
    assert "private-provider-body" not in output.read_text()
    with TestClient(create_app(Settings(database_url="sqlite:///:memory:"))) as client:
        assert client.get("/api/v1/metrics").status_code == 404


def test_abstention_counter_tracks_audited_outcome() -> None:
    """A safe unsupported-topic refusal increments abstention without counting a stage failure."""
    app = create_app(Settings(database_url="sqlite:///:memory:", telemetry_enabled=True))
    with TestClient(app) as client:
        result = client.post(
            "/api/v1/query",
            json={
                "borrower_id": "borrower-001",
                "question": "What is tomorrow's weather?",
                "effective_at": "2026-06-01",
            },
        )
        assert result.status_code == 200
        assert result.json()["abstained"]
        metrics = app.state.telemetry.render().decode()
        assert "creditlens_abstentions_total 1.0" in metrics
        assert "creditlens_stage_errors_total{" not in metrics
