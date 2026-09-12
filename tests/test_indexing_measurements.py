"""Indexing evidence must not turn partial writes or timeouts into successful freshness."""

import json
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx
import pytest

from creditlens.corpus import borrower_pages
from creditlens.retrieval import chunk_page
from scripts import measure_indexing
from scripts.indexing_measurements import (
    bulk_outcomes,
    poll_visibility,
    prometheus_snapshot,
    throughput,
    upload,
)


def item(identity: str, status: int = 201) -> dict[str, Any]:
    """Construct the exact successful bulk response shape used by OpenSearch."""
    return {"index": {"_id": identity, "_index": "drill", "status": status}}


def test_bulk_mixed_rejection_is_not_transport_success() -> None:
    """An HTTP-successful bulk response can still reject individual chunks."""
    rejected = item("b", 400)
    rejected["index"]["error"] = {"type": "strict_dynamic_mapping_exception"}
    result = bulk_outcomes({"errors": True, "items": [item("a"), rejected]}, ("a", "b"), "drill")
    assert result == {"a": "acknowledged", "b": "rejected"}


@pytest.mark.parametrize(
    "body",
    [
        None,
        {"errors": False, "items": []},
        {"errors": False, "items": [item("other")]},
        {"errors": True, "items": [item("a")]},
        {"errors": False, "items": [{"index": {"_id": "a", "_index": "wrong", "status": 201}}]},
        {"errors": False, "items": [item("a", True)]},
        {"errors": False, "items": [item("a", 302)]},
    ],
)
def test_malformed_bulk_remains_unconfirmed(body: Any) -> None:
    """Missing, foreign, contradictory and malformed item responses never count as writes."""
    assert bulk_outcomes(body, ("a",), "drill") == {"a": "unconfirmed"}


def test_throughput_requires_complete_input_page_and_document() -> None:
    """A two-chunk page with one rejected chunk contributes zero successful pages/documents."""
    page = borrower_pages(1)[0]
    chunks = chunk_page(page.model_copy(update={"text": "x" * 1500}))
    outcomes = {chunks[0].chunk_id: "acknowledged", chunks[1].chunk_id: "rejected"}
    measured = throughput(chunks, outcomes, 2)
    assert measured["acknowledged_input_units"] == {"chunks": 1, "pages": 0, "documents": 0}
    assert measured["acknowledged_input_units_per_second"]["chunks"] == 0.5
    with pytest.raises(ValueError):
        throughput(chunks, outcomes, 0)
    with pytest.raises(ValueError):
        throughput((chunks[0], chunks[0]), outcomes, 1)


def test_poll_timeout_has_no_zero_lag_and_emits_no_lag_metric() -> None:
    """An invisible canary has an explicit failure state and absent visibility observation."""
    now = [0.0]

    def advance(seconds: float) -> None:
        """Advance an injected monotonic clock without sleeping in a unit test."""
        now[0] += seconds

    report = poll_visibility(lambda: False, 0, timeout=0.2, clock=lambda: now[0], pause=advance)
    assert report["status"] == "timeout"
    assert report["ack_to_observed_search_seconds"] is None
    scrape = prometheus_snapshot({"canary": report})
    assert "canary_visible 0" in scrape
    assert "visibility_seconds" not in scrape
    visible = poll_visibility(lambda: True, 0, clock=lambda: 0.1)
    assert visible["ack_to_observed_search_seconds"] == 0.1
    with pytest.raises(ValueError):
        poll_visibility(lambda: True, 0, timeout=0)


def test_transport_failure_keeps_item_ids_unconfirmed() -> None:
    """A transport error records its bounded type and never assumes successful indexing."""
    chunks = chunk_page(borrower_pages(1)[0])

    def fail(request: httpx.Request) -> httpx.Response:
        """Inject a timeout into the real upload transport seam."""
        raise httpx.ReadTimeout("secret must not be retained", request=request)

    records: list[dict[str, Any]] = []
    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        outcomes = upload(client, "http://127.0.0.1:19201", "drill", chunks, records)
    assert set(outcomes.values()) == {"unconfirmed"}
    assert records[0]["transport_failure"] is True
    assert "secret" not in json.dumps(records)


def test_slow_probe_cannot_report_visible_after_deadline() -> None:
    """A completed slow HTTP query cannot convert a passed deadline into a successful canary."""
    now = [0.0]

    def delayed() -> bool:
        """Simulate a search response arriving after the observation budget."""
        now[0] = 2
        return True

    result = poll_visibility(delayed, 0, timeout=1, clock=lambda: now[0], pause=lambda _: None)
    assert result["status"] == "timeout"
    assert result["ack_to_observed_search_seconds"] is None


def test_cleanup_failure_is_durable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed index creation followed by failed deletion preserves both failures."""

    def version(self: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
        """Return the service version without a network call."""
        return httpx.Response(200, json={"version": {}}, request=httpx.Request("GET", url))

    def fail(self: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
        """Inject the failure after the index creation attempt is recorded."""
        raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(httpx.Client, "get", version)
    monkeypatch.setattr(httpx.Client, "put", fail)
    monkeypatch.setattr(httpx.Client, "delete", fail)
    with tempfile.TemporaryDirectory() as temporary:
        pages = Path(temporary) / "pages.jsonl"
        pages.write_text(borrower_pages(1)[0].model_dump_json())
        output = Path(temporary) / "evidence"
        report = measure_indexing.run(pages, output)
        assert report["status"] == "fail"
        assert report["index_deleted"] is False
        assert report["cleanup_error_type"] == "ConnectError"
        assert json.loads((output / "report.json").read_text())["index_deleted"] is False


def test_initial_http_failure_preserves_private_failed_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before index creation, network failure still leaves a durable failed run artifact."""

    def fail(self: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
        """Reject the version request without connecting to any service."""
        raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(httpx.Client, "get", fail)
    with tempfile.TemporaryDirectory() as temporary:
        pages = Path(temporary) / "pages.jsonl"
        pages.write_text(borrower_pages(1)[0].model_dump_json())
        output = Path(temporary) / "evidence"
        report = measure_indexing.run(pages, output)
        assert report["status"] == "fail"
        assert report["index_deleted"] is True
        assert report["source_changed"] is False
        assert json.loads((output / "report.json").read_text())["error_type"] == "ConnectError"
        manifest = json.loads((output / "manifest.json").read_text())
        assert b"\r" not in (output / "metrics.prom").read_bytes()
        assert (
            manifest["metrics_sha256"] == sha256((output / "metrics.prom").read_bytes()).hexdigest()
        )
