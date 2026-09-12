"""Bounded indexing-drill measurements distinguish acknowledgments from search visibility."""

import json
from collections import defaultdict
from collections.abc import Callable
from time import perf_counter, sleep
from typing import Any

import httpx

from creditlens.cortex_search import index_record
from creditlens.domain import Chunk
from creditlens.opensearch_provider import decode_hits


def bulk_outcomes(body: Any, ids: tuple[str, ...], index: str) -> dict[str, str]:
    """Require exact per-item identities; malformed batches leave every operation unconfirmed."""
    unknown = dict.fromkeys(ids, "unconfirmed")
    if not isinstance(body, dict) or type(body.get("errors")) is not bool:
        return unknown
    items = body.get("items")
    if not isinstance(items, list) or len(items) != len(ids):
        return unknown
    result = {}
    for expected, item in zip(ids, items, strict=True):
        if not isinstance(item, dict) or set(item) != {"index"}:
            return unknown
        row = item["index"]
        if (
            not isinstance(row, dict)
            or row.get("_id") != expected
            or row.get("_index") != index
            or type(row.get("status")) is not int
        ):
            return unknown
        status = row["status"]
        if status in (200, 201) and "error" not in row:
            result[expected] = "acknowledged"
        elif 400 <= status < 600 and "error" in row:
            result[expected] = "rejected"
        else:
            return unknown
    if body["errors"] != ("rejected" in result.values()):
        return unknown
    return result


def upload(
    client: httpx.Client,
    endpoint: str,
    index: str,
    chunks: tuple[Chunk, ...],
    records: list[dict[str, Any]],
    *,
    inject_mapping_failure: bool = False,
) -> dict[str, str]:
    """Measure each actual bounded bulk request, retaining transport failures as unconfirmed."""
    outcomes: dict[str, str] = {}
    for start in range(0, len(chunks), 100):
        batch = chunks[start : start + 100]
        ids = tuple(chunk.chunk_id for chunk in batch)
        lines: list[str] = []
        for chunk in batch:
            source = index_record(chunk) | {"SEARCH_TEXT": chunk.text}
            if inject_mapping_failure:
                source["UNMAPPED_FAULT_FIELD"] = True
            lines.extend(
                (
                    json.dumps({"index": {"_index": index, "_id": chunk.chunk_id}}),
                    json.dumps(source),
                )
            )
        started = perf_counter()
        record: dict[str, Any] = {"expected_ids": ids, "transport_failure": False}
        try:
            response = client.post(
                f"{endpoint}/_bulk",
                content="\n".join(lines) + "\n",
                headers={"Content-Type": "application/x-ndjson"},
                params={"refresh": "false"},
            )
            response.raise_for_status()
            parsed = response.json()
            current = bulk_outcomes(parsed, ids, index)
            record["items"] = parsed
        except (httpx.HTTPError, ValueError) as error:
            current = dict.fromkeys(ids, "unconfirmed")
            record.update(transport_failure=True, error_type=type(error).__name__)
        record.update(duration_seconds=perf_counter() - started, outcomes=current)
        records.append(record)
        outcomes.update(current)
    return outcomes


def throughput(
    chunks: tuple[Chunk, ...], outcomes: dict[str, str], seconds: float
) -> dict[str, Any]:
    """Count only wholly acknowledged input units; partial pages/documents are not successes."""
    if seconds <= 0 or len({c.chunk_id for c in chunks}) != len(chunks):
        raise ValueError("Positive duration and unique input chunks are required")
    pages: dict[tuple[str, ...], set[str]] = defaultdict(set)
    documents: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for chunk in chunks:
        document = (chunk.tenant_id, chunk.document_id, chunk.document_version)
        documents[document].add(chunk.chunk_id)
        pages[(*document, str(chunk.page))].add(chunk.chunk_id)
    acknowledged = {c.chunk_id for c in chunks if outcomes.get(c.chunk_id) == "acknowledged"}
    counts = {
        "chunks": len(acknowledged),
        "pages": sum(ids <= acknowledged for ids in pages.values()),
        "documents": sum(ids <= acknowledged for ids in documents.values()),
    }
    return {
        "duration_seconds": seconds,
        "acknowledged_input_units": counts,
        "acknowledged_input_units_per_second": {k: v / seconds for k, v in counts.items()},
        "chunk_outcomes": {
            state: sum(outcomes.get(c.chunk_id, "unconfirmed") == state for c in chunks)
            for state in ("acknowledged", "rejected", "unconfirmed")
        },
    }


def visible(client: httpx.Client, endpoint: str, index: str, chunk: Chunk) -> bool:
    """Use search, not real-time GET, and validate the canary's complete canonical metadata."""
    response = client.post(
        f"{endpoint}/{index}/_search",
        json={
            "query": {"ids": {"values": [chunk.chunk_id]}},
            "_source": list(index_record(chunk)),
            "size": 1,
        },
        params={"allow_partial_search_results": "false"},
    )
    response.raise_for_status()
    return decode_hits(response.content, (chunk,), 1, index) == (chunk,)


def poll_visibility(
    probe: Callable[[], bool],
    acknowledged_at: float,
    *,
    timeout: float = 5,
    clock: Callable[[], float] = perf_counter,
    pause: Callable[[float], None] = sleep,
) -> dict[str, Any]:
    """Return an observed upper bound only on visibility; timeout never becomes zero lag."""
    if not 0 < timeout <= 30:
        raise ValueError("Canary timeout must be positive and at most thirty seconds")
    attempts = 0
    while clock() - acknowledged_at < timeout:
        attempts += 1
        observed = probe()
        if observed and clock() - acknowledged_at < timeout:
            return {
                "status": "visible",
                "attempts": attempts,
                "ack_to_observed_search_seconds": clock() - acknowledged_at,
            }
        pause(min(0.1, max(0, timeout - (clock() - acknowledged_at))))
    return {"status": "timeout", "attempts": attempts, "ack_to_observed_search_seconds": None}


def prometheus_snapshot(report: dict[str, Any]) -> str:
    """Export finite, identifier-free drill gauges; missing measurements are omitted."""
    lines = ["# Indexing drill snapshot, not a cumulative live exporter"]
    for phase in ("corpus", "fault"):
        if phase not in report:
            continue
        measured = report[phase]
        lines.append(
            f'creditlens_indexing_run_bulk_failures{{phase="{phase}"}} '
            f"{measured.get('bulk_failures', 0)}"
        )
        lines.append(
            f'creditlens_indexing_run_duration_seconds{{phase="{phase}"}} '
            f"{measured['duration_seconds']}"
        )
        for state, count in measured["chunk_outcomes"].items():
            lines.append(
                f'creditlens_indexing_run_chunks{{phase="{phase}",state="{state}"}} {count}'
            )
        for unit, value in measured["acknowledged_input_units_per_second"].items():
            lines.append(
                f'creditlens_indexing_run_units_per_second{{phase="{phase}",unit="{unit}"}} {value}'
            )
    canary = report.get("canary", {})
    if canary:
        lines.append(f"creditlens_indexing_canary_visible {int(canary['status'] == 'visible')}")
        delay = canary.get("ack_to_observed_search_seconds")
        if delay is not None:
            lines.append(f"creditlens_indexing_canary_visibility_seconds {delay}")
    return "\n".join(lines) + "\n"
