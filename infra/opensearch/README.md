# Local synthetic OpenSearch integration

This setup exercises the real OpenSearch lexical adapter. It has no cloud credentials
and is separate from the local Python BM25 control and from Cortex Search. The adapter
does not enable production API queries.

From the repository root:

```powershell
docker compose -f infra/opensearch/compose.yml up -d
curl.exe http://127.0.0.1:19200
uv run --no-sync python scripts/check_opensearch.py --pages ../resources/credit_lens/corpus/pages.jsonl --output ../resources/credit_lens/evals/opensearch-live-new
docker compose -f infra/opensearch/compose.yml down
```

Wait for the root endpoint to return a JSON version before running the integration.
The output directory must be new. The script creates a uniquely named index, writes
all canonical chunks from extracted physical pages, verifies every bulk operation,
and checks the resulting count. Queries exercise policy dates, borrower/tenant/ACL
scope, canonical citations and revocation while the revoked row remains in the index.
It saves actual transport requests, service responses, hashes, service version and
check results. Its index is deleted in a `finally` block. Compose teardown removes
the dedicated container and network; no data volume is attached.

The image pins OpenSearch 3.7.0 and its digest. The container has 2 CPUs, 1536 MiB of
memory and a 512 MiB JVM heap. Only port 9200 is published, at `127.0.0.1:19200`.
Security is disabled solely for this isolated synthetic test. Do not use this Compose
configuration for shared, external or production service access. [Official Docker
instructions](https://docs.opensearch.org/latest/install-and-configure/install-opensearch/docker/).

`index.json` defines one shard, zero replicas, standard text analysis and explicit
BM25 k1=1.2/b=0.75. It rejects unknown fields. The canonical projection comes from
`index_record`; SEARCH_TEXT is added only for indexing. Keyword/date fields provide
the exact tenant, borrower/shared-policy, ACL, half-open date and current chunk-ID
filters. Those restrictions run inside `bool.filter`, before lexical scoring.
[Boolean query documentation](https://docs.opensearch.org/latest/query-dsl/compound/bool/).

The adapter rejects shard failures, timeouts, wrong index/ID, malformed scores and
canonical metadata mismatches. It requests no partial results and uses service-side
timeout/cancellation plus finite HTTP socket timeouts. It has one attempt and no broad
fallback. A 1,024-ID allowlist and 1 MiB response cap match the Cortex adapter's current
scope limits. Hard wall-clock cancellation, shared catalog authority and managed
service authentication remain release work. [Search API
contract](https://docs.opensearch.org/latest/api-reference/search-apis/search/).

`HybridProvider` composes two explicitly configured providers that expose the exact
same authoritative `EvidenceCatalog` instance. Equal revision integers from distinct
catalogs do not satisfy this contract; replacing either catalog later is rejected. It
requests up to 100 candidates per branch and fuses ranks with RRF k=60. Both branches
must succeed under the same request, principal and catalog revision. Conflicting
canonical chunks fail the result. Verification and citation access retain both branch
freshness checks. Tests use a named dense provider double; that is composition evidence,
not a live Cortex/dense hybrid quality measurement. Current provider labels identify
each actual branch rather than replacing the unavailable branch with a fixture label.

The integration CLI writes its initial manifest before network access. Connection,
indexing, query and cleanup failures retain failed manifests and any collected
request/response records. Input, mapping, script and source hashes are checked again
at completion; changed inputs invalidate the run.
