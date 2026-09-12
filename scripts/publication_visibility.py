"""Measure complete canonical SQL publication through exact OpenSearch search visibility."""

import json
from collections.abc import Callable
from time import perf_counter, sleep
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.engine import Engine

from creditlens.cortex_search import index_record
from creditlens.domain import Chunk, Page
from creditlens.opensearch_provider import valid_shards
from creditlens.retrieval import chunk_page
from creditlens.sql_catalog import SqlEvidenceCatalog, chunks


def publish_all(
    engine: Engine,
    catalog: SqlEvidenceCatalog,
    pages: tuple[Page, ...],
    clock: Callable[[], float] = perf_counter,
) -> tuple[float, int]:
    """Capture time immediately after one actual commit returns, never before commit starts."""
    if not pages:
        raise ValueError("Publication requires physical pages")
    with engine.begin() as connection:
        connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
        connection.exec_driver_sql("SET LOCAL lock_timeout = '2s'")
        revision = 0
        for offset in range(0, len(pages), 500):
            revision = catalog.publish_in_transaction(connection, pages[offset : offset + 500])
    committed_at = clock()
    return committed_at, revision


def read_canonical(
    engine: Engine,
    catalog: SqlEvidenceCatalog,
    pages: tuple[Page, ...],
    revision: int,
) -> tuple[Chunk, ...]:
    """An owned index-writer scan must exactly match every frozen canonical input chunk."""
    expected = {c.chunk_id: c for page in pages for c in chunk_page(page)}
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(chunks.c.payload)
                .where(chunks.c.catalog_id == catalog.catalog_id)
                .order_by(chunks.c.chunk_id)
            )
            .scalars()
            .all()
        )
    actual = tuple(Chunk.model_validate(row) for row in rows)
    if len(actual) != len(expected) or {c.chunk_id: c for c in actual} != expected:
        raise ValueError("SQL canonical corpus differs from frozen input")
    catalog.verify_revision(revision)
    return actual


def exact_visible(body: Any, expected: tuple[Chunk, ...], index: str) -> set[str]:
    """Accept missing hits as pending, but reject every malformed or altered search response."""
    if (
        not isinstance(body, dict)
        or body.get("timed_out") is not False
        or body.get("terminated_early", False) is not False
        or not isinstance(body.get("_shards"), dict)
        or not valid_shards(body["_shards"])
    ):
        raise ValueError("Search did not complete successfully")
    hits = body.get("hits")
    if not isinstance(hits, dict) or not isinstance(hits.get("hits"), list):
        raise ValueError("Malformed search hits")
    allowed = {c.chunk_id: index_record(c) | {"SEARCH_TEXT": c.text} for c in expected}
    found: set[str] = set()
    for hit in hits["hits"]:
        if not isinstance(hit, dict) or not isinstance(hit.get("_id"), str):
            raise ValueError("Malformed search identity")
        identity = hit["_id"]
        if identity not in allowed or identity in found or hit.get("_index") != index:
            raise ValueError("Foreign or duplicate search identity")
        if json.dumps(hit.get("_source"), sort_keys=True) != json.dumps(
            allowed[identity], sort_keys=True
        ):
            raise ValueError("Search text or metadata differs from canonical evidence")
        found.add(identity)
    return found


def search_sweep(
    client: httpx.Client,
    endpoint: str,
    index: str,
    expected: tuple[Chunk, ...],
    records: list[dict[str, Any]],
    deadline: float,
    clock: Callable[[], float] = perf_counter,
) -> set[str]:
    """Verify all expected IDs through bounded searches in one sweep without real-time GET."""
    found: set[str] = set()
    for offset in range(0, len(expected), 100):
        if clock() >= deadline:
            break
        batch = expected[offset : offset + 100]
        body = {
            "query": {"ids": {"values": [c.chunk_id for c in batch]}},
            "size": len(batch),
            "_source": True,
            "timeout": "5s",
        }
        record: dict[str, Any] = {"expected_ids": [c.chunk_id for c in batch]}
        records.append(record)
        try:
            response = client.post(
                f"{endpoint}/{index}/_search",
                json=body,
                params={"allow_partial_search_results": "false", "request_cache": "false"},
            )
            response.raise_for_status()
            record["response"] = response.json()
            verified = exact_visible(record["response"], batch, index)
            record["verified_ids"] = sorted(verified)
            found.update(verified)
        except (httpx.HTTPError, ValueError) as error:
            record["error_type"] = type(error).__name__
            raise
    return found


def await_complete(
    probe: Callable[[float], set[str]],
    expected_ids: set[str],
    committed_at: float,
    sweeps: list[dict[str, Any]],
    *,
    timeout: float = 30,
    clock: Callable[[], float] = perf_counter,
    pause: Callable[[float], None] = sleep,
) -> dict[str, Any]:
    """Only one complete successful sweep establishes all-input visibility before the deadline."""
    if not expected_ids or not 0 < timeout <= 60:
        raise ValueError("A nonempty expected corpus and bounded deadline are required")
    deadline = committed_at + timeout
    found: set[str] = set()
    while clock() < deadline:
        found = probe(deadline)
        observed_at = clock()
        if not found <= expected_ids:
            raise ValueError("Visibility probe returned foreign evidence")
        sweeps.append(
            {
                "verified_chunks": len(found),
                "missing_ids": sorted(expected_ids - found),
                "seconds_since_commit": observed_at - committed_at,
            }
        )
        if found == expected_ids and observed_at < deadline:
            return {
                "status": "complete",
                "verified_chunks": len(found),
                "publication_to_all_search_seconds": observed_at - committed_at,
                "observed_monotonic": observed_at,
                "missing_ids": [],
            }
        pause(min(0.1, max(0, deadline - clock())))
    return {
        "status": "timeout",
        "verified_chunks": len(found),
        "publication_to_all_search_seconds": None,
        "missing_ids": sorted(expected_ids - found),
    }


def metrics(report: dict[str, Any]) -> bytes:
    """Use fixed gauges and omit delay when complete search visibility was not measured."""
    visibility = report.get("visibility", {})
    values = {
        "creditlens_publication_drill_success": int(report["status"] == "pass"),
        "creditlens_publication_expected_chunks": report.get("expected_chunks", 0),
        "creditlens_publication_verified_chunks": visibility.get("verified_chunks", 0),
    }
    delay = visibility.get("publication_to_all_search_seconds")
    if visibility.get("status") == "complete" and delay is not None:
        values["creditlens_publication_all_input_visibility_seconds"] = delay
    lines: list[str] = []
    descriptions = {
        "creditlens_publication_drill_success": "Whether the complete local experiment passed.",
        "creditlens_publication_expected_chunks": "Frozen canonical chunks expected in search.",
        "creditlens_publication_verified_chunks": "Exact chunks verified in the last search sweep.",
        "creditlens_publication_all_input_visibility_seconds": (
            "SQL commit return to complete input search observation in this local fixture."
        ),
    }
    for name, value in values.items():
        lines.extend(
            (f"# HELP {name} {descriptions[name]}", f"# TYPE {name} gauge", f"{name} {value}")
        )
    return ("\n".join(lines) + "\n").encode("utf-8")
