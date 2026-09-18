"""Explicit administrative publication of one current SQL-authorized lexical scope."""

import json
from datetime import date
from typing import Any

from creditlens.domain import Chunk, Principal, QueryRequest
from creditlens.errors import ServiceError
from creditlens.governed_opensearch import GovernedOpenSearchProvider, record
from creditlens.sql_catalog import SqlEvidenceCatalog
from creditlens.storage import GrantStore


def verify_scope(
    catalog: SqlEvidenceCatalog, grants: GrantStore, principal: Principal, revision: int
) -> None:
    """Stop subsequent writes when canonical authority or the indexing grant has changed."""
    catalog.verify_revision(revision)
    if grants.resolve(principal.subject) != principal:
        raise ServiceError("access_changed", "Access changed; retry indexing", 409)


def validate_publication(body: Any, batch: tuple[Chunk, ...], index: str) -> None:
    """HTTP success is insufficient: every ordered bulk item must confirm its exact write."""
    if not isinstance(body, dict) or body.get("errors") is not False:
        raise ValueError("Bulk indexing failed")
    items = body.get("items")
    if not isinstance(items, list) or len(items) != len(batch):
        raise ValueError("Incomplete bulk result")
    for item, chunk in zip(items, batch, strict=True):
        result = item.get("index") if isinstance(item, dict) else None
        if not isinstance(result, dict) or not valid_write(result, chunk.chunk_id, index):
            raise ValueError("Invalid bulk item")


def valid_write(result: dict[str, Any], chunk_id: str, index: str) -> bool:
    """Accept idempotent replacement while rejecting partial or misdirected acknowledgments."""
    shards = result.get("_shards")
    if not isinstance(shards, dict) or any(
        type(shards.get(key)) is not int for key in ("total", "successful", "failed")
    ):
        return False
    return bool(
        result.get("_index") == index
        and result.get("_id") == chunk_id
        and type(result.get("status")) is int
        and result["status"] in (200, 201)
        and result.get("result") in ("created", "updated")
        and "error" not in result
        and shards["failed"] == 0
        and 1 <= shards["successful"] <= shards["total"]
    )


def publish_batch(index: GovernedOpenSearchProvider, batch: tuple[Chunk, ...]) -> None:
    """Send bounded idempotent writes to one fixed index and wait for search visibility."""
    if not batch or len(batch) > 100 or len({chunk.chunk_id for chunk in batch}) != len(batch):
        raise ValueError("Expected one nonempty unique indexing batch of at most 100 records")
    lines = []
    for chunk in batch:
        lines.append(json.dumps({"index": {"_id": chunk.chunk_id}}, separators=(",", ":")))
        lines.append(json.dumps(record(chunk), separators=(",", ":")))
    try:
        body = index.request(
            "POST",
            f"/{index.index}/_bulk?refresh=wait_for",
            content=("\n".join(lines) + "\n").encode(),
        )
        validate_publication(body, batch, index.index)
    except (ValueError, TypeError, KeyError) as error:
        raise ServiceError("search_index_failed", "Lexical indexing failed") from error


def synchronize(
    catalog: SqlEvidenceCatalog,
    grants: GrantStore,
    index: GovernedOpenSearchProvider,
    subject: str,
    borrower: str,
    effective_at: date,
    *,
    create_index: bool = False,
) -> int:
    """Publish an admin's scoped snapshot; queries keep rejecting any incomplete index."""
    if (
        index.catalog is not catalog
        or index.store is not grants
        or catalog.engine.pool is not grants.engine.pool
        or index.namespace != catalog.catalog_id
        or index.authority != catalog.authority_id
    ):
        raise ValueError("Indexing requires the same bound SQL catalog and grant store")
    principal = grants.resolve(subject)
    if principal.role != "admin":
        raise ServiceError("access_denied", "Indexing requires an administrator", 403)
    request = QueryRequest(
        borrower_id=borrower, question="index synchronization", effective_at=effective_at
    )
    candidates, revision = catalog.snapshot(principal, borrower, effective_at)
    verify_scope(catalog, grants, principal, revision)
    if create_index:
        created = index.request("PUT", f"/{index.index}", index.schema())
        if not isinstance(created, dict) or not (
            created.get("acknowledged") is True
            and created.get("shards_acknowledged") is True
            and created.get("index") == index.index
        ):
            raise ServiceError("search_index_failed", "Lexical index creation failed")
    index.check_ready()
    for offset in range(0, len(candidates), 100):
        verify_scope(catalog, grants, principal, revision)
        publish_batch(index, candidates[offset : offset + 100])
    verify_scope(catalog, grants, principal, revision)
    # The reader checks every canonical row even when no lexical term matches this probe.
    index.search(request, principal, 1)
    verify_scope(catalog, grants, principal, revision)
    return len(candidates)
