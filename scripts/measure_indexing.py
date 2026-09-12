"""Run an actual loopback OpenSearch indexing drill and preserve completed or failed evidence."""

import argparse
import json
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import httpx

from creditlens.domain import Citation, Page, QueryRequest
from creditlens.opensearch_provider import OpenSearchProvider
from creditlens.retrieval import EvidenceCatalog, chunk_page
from creditlens.storage import GrantStore, open_database
from scripts.indexing_measurements import (
    poll_visibility,
    prometheus_snapshot,
    throughput,
    upload,
    visible,
)

REPO = Path(__file__).resolve().parents[1]
ENDPOINT = "http://127.0.0.1:19201"


def require(condition: bool, message: str) -> None:
    """Keep drill checks active under Python optimization without publishing raw server errors."""
    if not condition:
        raise RuntimeError(message)


def hashes(pages: Path) -> dict[str, str]:
    """Hash the actual measurement path, canonical inputs and mapping at both run boundaries."""
    paths = [
        pages,
        Path(__file__),
        REPO / "scripts/indexing_measurements.py",
        REPO / "infra/opensearch/index.json",
        *sorted((REPO / "src").rglob("*.py")),
    ]
    return {
        str(path.relative_to(REPO)) if path.is_relative_to(REPO) else "pages.jsonl": sha256(
            path.read_bytes()
        ).hexdigest()
        for path in paths
    }


def canary_page() -> Page:
    """Author an isolated synthetic source whose exact token is absent before indexing."""
    token = "indexcanary" + uuid4().hex
    return Page(
        tenant_id="demo-bank",
        borrower_id="borrower-001",
        document_id=token,
        document_version="v1",
        page=1,
        document_kind="monitoring_canary",
        title="Index canary",
        section="monitoring",
        text=token,
        acl_groups=("underwriting",),
        valid_from=date(2026, 1, 1),
        content_hash=sha256(token.encode()).hexdigest(),
        parser_version="authored-canary-v1",
    )


def canary_checks(
    client: httpx.Client, index: str, report: dict[str, Any], records: list[dict[str, Any]]
) -> None:
    """Observe refresh visibility, then prove indexed stale evidence remains permission-denied."""
    page = canary_page()
    chunk = chunk_page(page)[0]
    report["canary_absent_before_write"] = not visible(client, ENDPOINT, index, chunk)
    require(report["canary_absent_before_write"], "Canary existed before its write")
    outcomes = upload(client, ENDPOINT, index, (chunk,), records)
    acknowledged_at = perf_counter()
    require(outcomes[chunk.chunk_id] == "acknowledged", "Canary was not acknowledged")
    report["canary"] = poll_visibility(
        lambda: visible(client, ENDPOINT, index, chunk), acknowledged_at
    )
    require(report["canary"]["status"] == "visible", "Canary visibility timed out")
    engine = open_database("sqlite:///:memory:")
    try:
        store = GrantStore(engine)
        store.seed_demo()
        principal = store.resolve("synthetic-demo")
        catalog = EvidenceCatalog((page,))
        provider = OpenSearchProvider(
            ENDPOINT, index, client, catalog, store, allow_local_http=True
        )
        request = QueryRequest(
            borrower_id="borrower-001", question=page.text, effective_at=date(2026, 9, 1)
        )
        result = provider.search(request, principal)
        require(result.chunks == (chunk,), "Authorized canary was not returned")
        citation = Citation(
            chunk_id=chunk.chunk_id,
            document_id=page.document_id,
            document_version=page.document_version,
            page=1,
        )
        require(provider.citation(result, citation) == chunk, "Canary provenance failed")
        catalog.revoke(chunk.chunk_id)
        report["canary_revoked_but_search_visible"] = visible(client, ENDPOINT, index, chunk)
        report["canary_revoked_authorized_results"] = len(
            provider.search(request, principal).chunks
        )
        require(report["canary_revoked_but_search_visible"], "Expected stale canary in index")
        require(report["canary_revoked_authorized_results"] == 0, "Revoked canary leaked")
    finally:
        engine.dispose()
    fault = chunk_page(canary_page())
    started = perf_counter()
    outcomes = upload(client, ENDPOINT, index, fault, records, inject_mapping_failure=True)
    report["fault"] = throughput(fault, outcomes, perf_counter() - started)
    report["fault"]["bulk_failures"] = int(
        any(state != "acknowledged" for state in outcomes.values())
    )
    require(report["fault"]["chunk_outcomes"]["rejected"] == 1, "Mapping fault was not rejected")


def save(output: Path, report: dict[str, Any], records: list[dict[str, Any]]) -> None:
    """Persist partial results independently of HTTP failure or remote cleanup success."""
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (output / "bulk.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in records), encoding="utf-8"
    )
    (output / "metrics.prom").write_bytes(prometheus_snapshot(report).encode("utf-8"))
    manifest = {
        "status": report["status"],
        "scope": "indexing_drill_snapshot",
        "metrics_sha256": sha256((output / "metrics.prom").read_bytes()).hexdigest(),
        "report_sha256": sha256((output / "report.json").read_bytes()).hexdigest(),
        "bulk_sha256": sha256((output / "bulk.jsonl").read_bytes()).hexdigest(),
        "source_hashes": report["source_hashes"],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def run(pages_path: Path, output: Path) -> dict[str, Any]:
    """Mutate one fresh owned index and always preserve failure, hashes and deletion outcome."""
    output = output.resolve()
    if output.is_relative_to(REPO):
        raise ValueError("Private drill evidence must be outside the repository")
    output.mkdir(parents=True, exist_ok=False)
    index = "creditlens-indexing-" + uuid4().hex
    report: dict[str, Any] = {
        "status": "running",
        "started_utc": datetime.now(UTC).isoformat(),
        "scope": "actual_loopback_opensearch_synthetic_drill",
        "index": index,
        "refresh": "automatic; no forced refresh",
        "source_hashes": hashes(pages_path),
        "end_to_end_publication_lag": "unmeasured",
        "embedding_failures": "unmeasured",
        "authority": "memory fixture with fresh SQLite grants; not a SQL durability test",
    }
    records: list[dict[str, Any]] = []
    save(output, report, records)
    with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
        attempted = False
        try:
            pages = tuple(
                Page.model_validate_json(line) for line in pages_path.read_text().splitlines()
            )
            chunks = tuple(chunk for page in pages for chunk in chunk_page(page))
            require(bool(chunks), "Corpus is empty")
            require(len({c.chunk_id for c in chunks}) == len(chunks), "Duplicate input chunks")
            response = client.get(ENDPOINT)
            response.raise_for_status()
            report["service"] = response.json()["version"]
            mapping = json.loads((REPO / "infra/opensearch/index.json").read_text())
            attempted = True
            client.put(f"{ENDPOINT}/{index}", json=mapping).raise_for_status()
            started = perf_counter()
            outcomes = upload(client, ENDPOINT, index, chunks, records)
            report["corpus"] = throughput(chunks, outcomes, perf_counter() - started)
            report["corpus"]["bulk_failures"] = sum(
                any(state != "acknowledged" for state in record["outcomes"].values())
                for record in records
            )
            save(output, report, records)
            require(
                all(value == "acknowledged" for value in outcomes.values()), "Corpus bulk failure"
            )
            canary_checks(client, index, report, records)
            report["status"] = "pass"
        except Exception as error:
            report.update(status="fail", error_type=type(error).__name__)
        finally:
            report["index_deleted"] = not attempted
            if attempted:
                try:
                    cleanup = client.delete(f"{ENDPOINT}/{index}")
                    report["index_deleted"] = cleanup.status_code in (200, 404)
                except httpx.HTTPError as error:
                    report["cleanup_error_type"] = type(error).__name__
            try:
                report["end_source_hashes"] = hashes(pages_path)
                report["source_changed"] = report["source_hashes"] != report["end_source_hashes"]
            except OSError:
                report["source_changed"] = True
            if not report["index_deleted"] or report["source_changed"]:
                report["status"] = "fail"
            report["completed_utc"] = datetime.now(UTC).isoformat()
            save(output, report, records)
    return report


def main() -> None:
    """Require existing physical-page input and a fresh private output directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.pages, args.output)
    print(json.dumps({"status": report["status"], "output": str(args.output)}))
    require(report["status"] == "pass", "Indexing drill failed; inspect private evidence")


if __name__ == "__main__":
    main()
