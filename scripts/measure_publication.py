"""Run the frozen physical-corpus SQL-publication to complete-search-visibility experiment."""

import argparse
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from creditlens.domain import Page
from creditlens.sql_catalog import chunks, initialize_catalog, page_acls, pages, states
from scripts.indexing_measurements import upload
from scripts.publication_visibility import (
    await_complete,
    metrics,
    publish_all,
    read_canonical,
    search_sweep,
)

REPO = Path(__file__).resolve().parents[1]
ENDPOINT = "http://127.0.0.1:19202"
DATABASE = (
    "postgresql+psycopg://postgres:creditlens-local-fixture-only@127.0.0.1:15434/"
    "creditlens_publication_fixture"
)
FROZEN_PAGES_SHA256 = "2eda960689e688289229a72bf46d9e3956b8907dd442cc586ce4b449f284f240"


def source_hashes(source: Path) -> dict[str, str]:
    """Pin the complete imported experiment path while unrelated runtime work may continue."""
    files = [
        Path(__file__),
        REPO / "scripts/publication_visibility.py",
        REPO / "scripts/indexing_measurements.py",
        REPO / "infra/opensearch/index.json",
        REPO / "infra/monitoring/publication-compose.yml",
        *[
            REPO / "src/creditlens" / f"{name}.py"
            for name in (
                "sql_catalog",
                "domain",
                "retrieval",
                "cortex_search",
                "opensearch_provider",
                "auth",
                "access",
                "storage",
                "settings",
                "errors",
                "search_provider",
            )
        ],
    ]
    return {
        str(file.relative_to(REPO)): sha256(file.read_bytes()).hexdigest() for file in files
    } | {"physical_pages_jsonl": sha256(source.read_bytes()).hexdigest()}


def save(output: Path, report: dict[str, Any], evidence: dict[str, list[dict[str, Any]]]) -> None:
    """Preserve failure and partial evidence with LF metric bytes and a verifiable manifest."""
    (output / "report.json").write_bytes((json.dumps(report, indent=2) + "\n").encode())
    for name, records in evidence.items():
        (output / f"{name}.jsonl").write_bytes(
            "".join(json.dumps(row) + "\n" for row in records).encode()
        )
    (output / "metrics.prom").write_bytes(metrics(report))
    hashed = {
        file.name: sha256(file.read_bytes()).hexdigest()
        for file in output.iterdir()
        if file.is_file() and file.name != "manifest.json"
    }
    manifest = {
        "status": report["status"],
        "scope": report["scope"],
        "metrics_sha256": hashed["metrics.prom"],
        "files_sha256": hashed,
        "source_hashes": report["source_hashes"],
    }
    (output / "manifest.json").write_bytes((json.dumps(manifest, indent=2) + "\n").encode())


def execute(
    engine: Engine,
    client: httpx.Client,
    source: tuple[Page, ...],
    report: dict[str, Any],
    evidence: dict[str, list[dict[str, Any]]],
) -> None:
    """Keep the complete visibility interval anchored to confirmed SQL commit return."""
    catalog = initialize_catalog(engine, report["catalog_id"])
    report["authority_before_publication"] = catalog.version
    started = perf_counter()
    committed_at, revision = publish_all(engine, catalog, source)
    report.update(
        committed_monotonic=committed_at,
        committed_revision=revision,
        sql_publication_duration_seconds=committed_at - started,
    )
    canonical = read_canonical(engine, catalog, source, revision)
    report.update(
        expected_chunks=len(canonical),
        expected_pages=len(source),
        expected_document_versions=len(
            {(p.tenant_id, p.document_id, p.document_version) for p in source}
        ),
        authority_after_publication=catalog.version,
    )
    evidence["canonical"] = [
        {
            "chunk_id": c.chunk_id,
            "canonical_payload_sha256": sha256(c.model_dump_json().encode()).hexdigest(),
        }
        for c in canonical
    ]
    report["visibility"] = {
        "status": "unconfirmed",
        "verified_chunks": 0,
        "publication_to_all_search_seconds": None,
        "missing_ids": [c.chunk_id for c in canonical],
    }
    outcomes = upload(client, ENDPOINT, report["index"], canonical, evidence["bulk"])
    report["bulk_acknowledged"] = sum(value == "acknowledged" for value in outcomes.values())
    if len(outcomes) != len(canonical) or any(
        value != "acknowledged" for value in outcomes.values()
    ):
        raise ValueError("Corpus bulk write was rejected or unconfirmed")

    def probe(deadline: float) -> set[str]:
        """A full sweep checks every frozen ID and exact text/metadata in actual search hits."""
        return search_sweep(
            client, ENDPOINT, report["index"], canonical, evidence["search"], deadline
        )

    report["visibility"] = await_complete(
        probe, {c.chunk_id for c in canonical}, committed_at, evidence["sweeps"]
    )
    catalog.verify_revision(revision)
    report["authority_after_visibility"] = catalog.version
    if report["visibility"]["status"] != "complete":
        raise ValueError("Complete search visibility was not observed within the budget")


def cleanup(engine: Engine, client: httpx.Client, report: dict[str, Any]) -> None:
    """Delete only this experiment's random index and catalog rows, preserving failed cleanup."""
    report["index_deleted"] = False
    report["catalog_deleted"] = False
    try:
        response = client.delete(f"{ENDPOINT}/{report['index']}")
        report["index_deleted"] = response.status_code in (200, 404)
    except httpx.HTTPError as error:
        report["index_cleanup_error_type"] = type(error).__name__
    try:
        with engine.begin() as connection:
            for table in (chunks, page_acls, pages, states):
                connection.execute(table.delete().where(table.c.catalog_id == report["catalog_id"]))
        report["catalog_deleted"] = True
    except Exception as error:
        report["catalog_cleanup_error_type"] = type(error).__name__


def run(source: Path, output: Path) -> dict[str, Any]:
    """Require the frozen corpus and preserve all measurements before any network mutation."""
    output = output.resolve()
    if output.is_relative_to(REPO):
        raise ValueError("Evidence must remain outside the repository")
    raw = source.read_bytes()
    if sha256(raw).hexdigest() != FROZEN_PAGES_SHA256:
        raise ValueError("Physical corpus differs from the frozen input")
    physical = tuple(Page.model_validate_json(line) for line in raw.decode().splitlines())
    if len(physical) != 3840:
        raise ValueError("Expected all 3840 frozen physical pages")
    output.mkdir(parents=True, exist_ok=False)
    identity = uuid4().hex
    report: dict[str, Any] = {
        "scope": "actual_local_sql_publication_to_all_frozen_input_search_visibility",
        "status": "running",
        "started_utc": datetime.now(UTC).isoformat(),
        "catalog_id": "publication-" + identity,
        "index": "creditlens-publication-" + identity,
        "source_hashes": source_hashes(source),
        "visibility_budget_seconds": 30,
        "refresh": "automatic_only",
        "measurement": "fixture_observed_upper_bound_not_production_sla",
    }
    evidence: dict[str, list[dict[str, Any]]] = {
        name: [] for name in ("canonical", "bulk", "search", "sweeps")
    }
    save(output, report, evidence)
    engine = create_engine(DATABASE, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
        try:
            response = client.get(ENDPOINT)
            response.raise_for_status()
            report["opensearch_version"] = response.json()["version"]
            with engine.connect() as connection:
                report["postgres_version"] = connection.exec_driver_sql(
                    "SELECT version()"
                ).scalar_one()
            mapping = json.loads((REPO / "infra/opensearch/index.json").read_text())
            client.put(f"{ENDPOINT}/{report['index']}", json=mapping).raise_for_status()
            execute(engine, client, physical, report, evidence)
            report["status"] = "pass"
        except Exception as error:
            report.update(status="fail", error_type=type(error).__name__)
        finally:
            cleanup(engine, client, report)
            engine.dispose()
            try:
                report["end_source_hashes"] = source_hashes(source)
                report["source_changed"] = report["source_hashes"] != report["end_source_hashes"]
            except OSError:
                report["source_changed"] = True
            if (
                not report["index_deleted"]
                or not report["catalog_deleted"]
                or report["source_changed"]
            ):
                report["status"] = "fail"
            report["completed_utc"] = datetime.now(UTC).isoformat()
            save(output, report, evidence)
    return report


def main() -> None:
    """Require explicit original corpus and a new private output directory for each run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.pages, args.output)
    print(json.dumps({"status": report["status"], "output": str(args.output)}))
    if report["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
