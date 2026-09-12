"""Whole-input visibility requires a completed sweep with exact text and canonical metadata."""

import copy
import json
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from creditlens.corpus import borrower_pages
from creditlens.cortex_search import index_record
from creditlens.retrieval import chunk_page
from scripts.measure_publication import cleanup, save
from scripts.publication_visibility import (
    await_complete,
    exact_visible,
    metrics,
    publish_all,
    read_canonical,
    search_sweep,
)

CHUNK = chunk_page(borrower_pages(1)[0])[0]


def response_body() -> dict[str, Any]:
    """Use the actual search response envelope and full index metadata plus text."""
    return {
        "timed_out": False,
        "_shards": {"total": 1, "successful": 1, "failed": 0},
        "hits": {
            "hits": [
                {
                    "_id": CHUNK.chunk_id,
                    "_index": "drill",
                    "_source": index_record(CHUNK) | {"SEARCH_TEXT": CHUNK.text},
                }
            ]
        },
    }


def test_exact_search_and_missing_results() -> None:
    """A missing result is pending; a complete exact result establishes that chunk's visibility."""
    body = response_body()
    assert exact_visible(body, (CHUNK,), "drill") == {CHUNK.chunk_id}
    body["hits"]["hits"] = []
    assert exact_visible(body, (CHUNK,), "drill") == set()


@pytest.mark.parametrize(
    "field,value",
    [
        ("SEARCH_TEXT", "altered searchable text"),
        ("TENANT_ID", "foreign"),
        ("PAGE", True),
        ("RECORD_SHA256", "f" * 64),
        ("ACL_GROUPS", ["foreign"]),
    ],
)
def test_altered_metadata_or_text_fails(field: str, value: Any) -> None:
    """Even correct IDs cannot conceal altered text, types, ACLs or payload fingerprints."""
    body = response_body()
    body["hits"]["hits"][0]["_source"][field] = value
    with pytest.raises(ValueError):
        exact_visible(body, (CHUNK,), "drill")


@pytest.mark.parametrize("change", ["timeout", "shards", "duplicate", "foreign", "terminated"])
def test_incomplete_or_foreign_envelope_fails(change: str) -> None:
    """A partial execution or duplicate/foreign hit invalidates the entire observation."""
    body = response_body()
    if change == "timeout":
        body["timed_out"] = True
    elif change == "shards":
        body["_shards"]["failed"] = 1
    elif change == "duplicate":
        body["hits"]["hits"].append(copy.deepcopy(body["hits"]["hits"][0]))
    elif change == "foreign":
        body["hits"]["hits"][0]["_index"] = "other"
    else:
        body["terminated_early"] = True
    with pytest.raises(ValueError):
        exact_visible(body, (CHUNK,), "drill")


def test_partial_sweeps_are_not_combined_into_full_coverage() -> None:
    """Seeing A then B in separate incomplete sweeps never establishes complete visibility."""
    now = [0.0]
    sweeps: list[dict[str, Any]] = []

    def partial(deadline: float) -> set[str]:
        """Alternate disjoint incomplete sets within the synthetic time budget."""
        now[0] += 0.2
        return {"a"} if len(sweeps) == 0 else {"b"}

    result = await_complete(
        partial, {"a", "b"}, 0, sweeps, timeout=0.5, clock=lambda: now[0], pause=lambda _: None
    )
    assert result["status"] == "timeout"
    assert result["verified_chunks"] == 1
    assert result["publication_to_all_search_seconds"] is None
    assert b"all_input_visibility_seconds" not in metrics({"status": "fail", "visibility": result})


def test_complete_sweep_after_deadline_is_not_successful() -> None:
    """The elapsed observation budget includes time spent in a slow search response."""
    now = [0.0]

    def late(deadline: float) -> set[str]:
        """Simulate a complete response that arrived too late."""
        now[0] = 2
        return {"a"}

    result = await_complete(
        late, {"a"}, 0, [], timeout=1, clock=lambda: now[0], pause=lambda _: None
    )
    assert result["status"] == "timeout"
    successful = await_complete(lambda _: {"a"}, {"a"}, 0, [], clock=lambda: 0.4)
    assert successful["publication_to_all_search_seconds"] == 0.4


def test_search_sweep_uses_search_and_exact_source() -> None:
    """Transport verification forbids a real-time GET or forced refresh substitute."""

    def handle(request: httpx.Request) -> httpx.Response:
        """Inspect the real HTTP request emitted by the experiment."""
        assert request.method == "POST"
        assert request.url.path == "/drill/_search"
        assert request.url.params["allow_partial_search_results"] == "false"
        assert json.loads(request.content)["_source"] is True
        return httpx.Response(200, json=response_body())

    records: list[dict[str, Any]] = []
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        found = search_sweep(
            client, "http://127.0.0.1", "drill", (CHUNK,), records, 1, clock=lambda: 0
        )
    assert found == {CHUNK.chunk_id}
    assert records[0]["verified_ids"] == [CHUNK.chunk_id]


def test_timestamp_occurs_after_commit_and_not_after_rollback() -> None:
    """A commit error must not produce a publication timestamp or a visibility start."""
    engine, catalog = MagicMock(), MagicMock()
    events: list[str] = []
    engine.begin.return_value.__exit__.side_effect = lambda *args: events.append("commit")
    catalog.publish_in_transaction.return_value = 9

    def clock() -> float:
        """Record timing order immediately after the transaction context returns."""
        events.append("clock")
        return 123.0

    assert publish_all(engine, catalog, borrower_pages(1)[:1], clock) == (123.0, 9)
    assert events == ["commit", "clock"]
    events.clear()
    engine.begin.return_value.__exit__.side_effect = RuntimeError("commit failed")
    with pytest.raises(RuntimeError):
        publish_all(engine, catalog, borrower_pages(1)[:1], clock)
    assert events == []


def test_export_lf_hashes_and_failed_visibility_omission() -> None:
    """Prometheus byte hashes match LF output and failed runs do not invent a delay."""
    with tempfile.TemporaryDirectory() as temporary:
        output = Path(temporary)
        report = {
            "status": "fail",
            "scope": "fixture",
            "source_hashes": {},
            "expected_chunks": 3840,
        }
        save(output, report, {"search": []})
        data = (output / "metrics.prom").read_bytes()
        assert b"\r" not in data
        assert b"all_input_visibility_seconds" not in data
        manifest = json.loads((output / "manifest.json").read_bytes())
        assert manifest["metrics_sha256"] == sha256(data).hexdigest()


def test_canonical_readback_rejects_incomplete_sql_rows() -> None:
    """Indexing cannot quietly omit physical pages that were expected to commit."""
    engine, catalog = MagicMock(), MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.scalars.return_value.all.return_value = []
    with pytest.raises(ValueError, match="differs"):
        read_canonical(engine, catalog, borrower_pages(1)[:1], 2)


def test_search_failure_retains_unconfirmed_record() -> None:
    """An unavailable search response cannot produce successful coverage or erase its attempt."""

    def unavailable(request: httpx.Request) -> httpx.Response:
        """Return an actual HTTP error envelope through the transport seam."""
        return httpx.Response(503, json={"error": "fixture"})

    records: list[dict[str, Any]] = []
    with httpx.Client(transport=httpx.MockTransport(unavailable)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            search_sweep(client, "http://127.0.0.1", "drill", (CHUNK,), records, 1, clock=lambda: 0)
    assert records[0]["error_type"] == "HTTPStatusError"
    assert "verified_ids" not in records[0]


def test_cleanup_failure_preserves_both_owned_resource_results() -> None:
    """Cleanup records independent SQL and search failures rather than declaring teardown done."""
    engine, client = MagicMock(), MagicMock()
    client.delete.side_effect = httpx.ConnectError("fixture")
    engine.begin.side_effect = RuntimeError("fixture")
    report: dict[str, Any] = {"index": "owned", "catalog_id": "owned"}
    cleanup(engine, client, report)
    assert report["index_deleted"] is False
    assert report["catalog_deleted"] is False
    assert report["index_cleanup_error_type"] == "ConnectError"
    assert report["catalog_cleanup_error_type"] == "RuntimeError"
