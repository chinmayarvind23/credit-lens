"""Index physical synthetic pages locally and preserve real OpenSearch integration evidence."""

import argparse
import json
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx

from creditlens.cortex_search import index_record
from creditlens.domain import Chunk, Citation, Page, QueryRequest
from creditlens.errors import ServiceError
from creditlens.evaluation import git_metadata, source_hashes
from creditlens.opensearch_provider import OpenSearchProvider
from creditlens.retrieval import EvidenceCatalog, chunk_page
from creditlens.storage import GrantStore, open_database

ENDPOINT = "http://127.0.0.1:19200"


class RecordingProvider(OpenSearchProvider):
    """Capture synthetic responses while executing the unchanged adapter transport."""

    records: list[dict[str, Any]]

    def _request(self, body: dict[str, Any]) -> bytes:
        """Record the actual service response without pretending a mocked fixture is live."""
        payload = super()._request(body)
        self.records.append({"request": body, "response": json.loads(payload)})
        return payload


def require(condition: bool, message: str) -> None:
    """Keep live integration assertions active even when Python optimization is enabled."""
    if not condition:
        raise RuntimeError(message)


def upload_chunks(client: httpx.Client, index: str, chunks: tuple[Chunk, ...]) -> None:
    """Bound bulk batches and verify every operation before declaring the index populated."""
    for start in range(0, len(chunks), 100):
        lines = []
        for chunk in chunks[start : start + 100]:
            lines.append(json.dumps({"index": {"_index": index, "_id": chunk.chunk_id}}))
            lines.append(json.dumps(index_record(chunk) | {"SEARCH_TEXT": chunk.text}))
        response = client.post(
            f"{ENDPOINT}/_bulk",
            content="\n".join(lines) + "\n",
            headers={"Content-Type": "application/x-ndjson"},
        )
        response.raise_for_status()
        require(response.json().get("errors") is False, "Bulk indexing failed")
    client.post(f"{ENDPOINT}/{index}/_refresh").raise_for_status()


def check_queries(provider: RecordingProvider, store: GrantStore) -> dict[str, Any]:
    """Exercise live version filters, restricted scope and stale index revocation."""
    principal = store.resolve("synthetic-demo")
    rankings = []
    for year, version in ((2025, "v1"), (2026, "v2"), (2027, "v3")):
        request = QueryRequest(
            borrower_id="borrower-001",
            question="minimum DSCR policy",
            effective_at=date(year, 9, 1),
        )
        result = provider.search(request, principal)
        require(bool(result.chunks), "Expected live policy results")
        require(
            all(c.document_version == version for c in result.chunks if c.borrower_id is None),
            "Live index returned a wrong policy version",
        )
        rankings.append({"year": year, "chunk_ids": [c.chunk_id for c in result.chunks]})
    request = QueryRequest(
        borrower_id="borrower-001", question="DSCR cash flow", effective_at=date(2026, 9, 1)
    )
    result = provider.search(request, principal)
    require(bool(result.chunks), "Expected current borrower evidence")
    require(
        all(
            c.tenant_id == "demo-bank"
            and c.borrower_id in (None, "borrower-001")
            and "underwriting" in c.acl_groups
            for c in result.chunks
        ),
        "Unauthorized result",
    )
    selected = result.chunks[0]
    citation = Citation(
        chunk_id=selected.chunk_id,
        document_id=selected.document_id,
        document_version=selected.document_version,
        page=selected.page,
    )
    require(provider.citation(result, citation) == selected, "Canonical citation mismatch")
    provider.catalog.revoke(selected.chunk_id)
    stale = provider.client.get(f"{ENDPOINT}/{provider.index}/_doc/{selected.chunk_id}")
    stale.raise_for_status()
    require(stale.json().get("found") is True, "Expected stale revoked row to remain indexed")
    revoked = provider.search(request, principal)
    require(selected.chunk_id not in {c.chunk_id for c in revoked.chunks}, "Revoked page returned")
    try:
        provider.citation(result, citation)
    except ServiceError as error:
        require(error.code == "evidence_changed", "Wrong citation revocation failure")
    else:
        raise RuntimeError("Revoked citation was accepted")
    count = len(provider.records)
    try:
        provider.search(request.model_copy(update={"borrower_id": "borrower-151"}), principal)
    except ServiceError as error:
        require(error.status == 403, "Wrong borrower denial")
    else:
        raise RuntimeError("Unauthorized borrower was accepted")
    require(len(provider.records) == count, "Denied borrower reached OpenSearch")
    restricted = provider.search(
        request.model_copy(update={"question": "RESTRICTED-001"}), principal
    )
    require(all("credit-officer" not in c.acl_groups for c in restricted.chunks), "Restricted leak")
    return {
        "version_rankings": rankings,
        "revoked_chunk_id": selected.chunk_id,
        "denied_borrower_http_calls": 0,
        "unauthorized_returned_chunks": 0,
        "canonical_citation": "pass",
        "stale_index_revocation": "pass",
        "revoked_row_still_indexed": True,
    }


def input_hashes(pages: Path, mapping: Path) -> dict[str, str]:
    """Bind every mutable input and the integration program to both ends of the run."""
    return {
        "pages_sha256": sha256(pages.read_bytes()).hexdigest(),
        "mapping_sha256": sha256(mapping.read_bytes()).hexdigest(),
        "script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def run_index_checks(
    client: httpx.Client,
    manifest: dict[str, Any],
    mapping: Path,
    pages: tuple[Page, ...],
    chunks: tuple[Chunk, ...],
    store: GrantStore,
    records: list[dict[str, Any]],
) -> None:
    """Preserve cleanup status even if indexing, query execution or index deletion fails."""
    index = manifest["index"]
    response = client.get(ENDPOINT)
    response.raise_for_status()
    manifest["service"] = response.json()
    manifest["index_creation_attempted"] = True
    try:
        client.put(f"{ENDPOINT}/{index}", json=json.loads(mapping.read_text())).raise_for_status()
        upload_chunks(client, index, chunks)
        count = client.get(f"{ENDPOINT}/{index}/_count")
        count.raise_for_status()
        require(count.json()["count"] == len(chunks), "Indexed document count mismatch")
        manifest["index_count"] = count.json()
        provider = RecordingProvider(
            ENDPOINT,
            index,
            client,
            EvidenceCatalog(pages),
            store,
            allow_local_http=True,
        )
        provider.records = records
        manifest["checks"] = check_queries(provider, store)
    finally:
        try:
            cleanup = client.delete(f"{ENDPOINT}/{index}")
            manifest["index_deleted"] = cleanup.status_code in (200, 404)
            manifest["cleanup_http_status"] = cleanup.status_code
        except httpx.HTTPError as error:
            manifest["index_deleted"] = False
            manifest["cleanup_error_type"] = type(error).__name__
    require(manifest["index_deleted"], "Integration index cleanup failed")


def save_evidence(
    output: Path,
    manifest: dict[str, Any],
    records: list[dict[str, Any]],
    transport_requests: list[dict[str, Any]],
) -> None:
    """Write completed or failed evidence independently of remote cleanup success."""
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for name, values in (("requests", records), ("transport-requests", transport_requests)):
        (output / f"{name}.jsonl").write_text("".join(json.dumps(row) + "\n" for row in values))


def main() -> None:
    """Mutate one new loopback test index and delete it after capturing evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[1]
    pages = tuple(Page.model_validate_json(line) for line in args.pages.read_text().splitlines())
    chunks = tuple(chunk for page in pages for chunk in chunk_page(page))
    index = "creditlens-contract-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    mapping = repo / "infra/opensearch/index.json"
    manifest: dict[str, Any] = {
        "time_utc": datetime.now(UTC).isoformat(),
        "git": git_metadata(repo),
        "source_hashes": source_hashes(repo),
        **input_hashes(args.pages, mapping),
        "index": index,
        "physical_pages": len(pages),
        "indexed_chunks": len(chunks),
        "integration_scope": "real_loopback_opensearch_synthetic_only",
        "semantic_quality": "unmeasured",
        "status": "running",
    }
    engine = open_database("sqlite:///:memory:")
    store = GrantStore(engine)
    store.seed_demo()
    records: list[dict[str, Any]] = []
    transport_requests: list[dict[str, Any]] = []

    def capture_request(request: httpx.Request) -> None:
        """Capture complete synthetic search requests after timeout and query options are added."""
        if request.url.path.endswith("/_search"):
            transport_requests.append(
                {
                    "method": request.method,
                    "url": str(request.url),
                    "body": json.loads(request.content),
                }
            )

    save_evidence(args.output, manifest, records, transport_requests)
    try:
        with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
            client.event_hooks["request"] = [capture_request]
            run_index_checks(client, manifest, mapping, pages, chunks, store, records)
        manifest["status"] = "pass"
    except Exception as error:
        manifest["status"] = "fail"
        manifest["error_type"] = type(error).__name__
    finally:
        engine.dispose()
        try:
            manifest["source_changed_during_run"] = manifest["source_hashes"] != source_hashes(repo)
            end_hashes = input_hashes(args.pages, mapping)
            manifest["end_input_hashes"] = end_hashes
            manifest["inputs_changed_during_run"] = any(
                manifest[key] != value for key, value in end_hashes.items()
            )
        except OSError as error:
            manifest["hash_error_type"] = type(error).__name__
            manifest["status"] = "fail"
        if manifest.get("source_changed_during_run") or manifest.get("inputs_changed_during_run"):
            manifest["status"] = "fail"
        save_evidence(args.output, manifest, records, transport_requests)
    print(json.dumps({"status": manifest["status"], "output": str(args.output)}, indent=2))
    require(manifest["status"] == "pass", "OpenSearch integration failed; inspect saved evidence")


if __name__ == "__main__":
    main()
