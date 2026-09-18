# OpenSearch lexical retrieval

The authenticated Weaviate path can use OpenSearch for its lexical branch. The server
combines its ranks with vector retrieval, reranks the combined evidence and passes
canonical PostgreSQL evidence to grounded generation. Select it with
`CREDITLENS_PRODUCTION_LEXICAL=opensearch` alongside
`CREDITLENS_PRODUCTION_SEARCH=weaviate`. The default lexical branch is local BM25.

## Configure the server

Set `CREDITLENS_OPENSEARCH_URL` to an HTTPS origin, `CREDITLENS_OPENSEARCH_INDEX` to one
physical index name, and `CREDITLENS_OPENSEARCH_TOKEN` to a reader bearer token issued
by your configured security provider. For a private CA, set
`CREDITLENS_OPENSEARCH_CA_FILE` to its PEM trust bundle. Certificate and hostname
verification remain enabled. The OpenSearch client has its own connection pool.

The index mapping binds to the existing catalog ID and its authority UUID. Readiness
checks that binding, the mapping, BM25 configuration, physical index UUID and search
permission. Aliases and a changed physical index cannot silently replace the selected
index. Restart the service after an intentional index replacement.

For every query, SQL first selects the authorized canonical scope. A single bounded
OpenSearch query returns that complete scope with lexical scores. The server checks
all source text and provenance before keeping matching results. An incomplete or
stale index fails the request. Grant changes and source revisions are checked again
before evidence is returned. Both lexical and vector branches are required when
OpenSearch is selected.

## Publish an authorized scope

Provision the SQL catalog and administrator grant first. In a separate indexing
process, load a writer token into `CREDITLENS_OPENSEARCH_TOKEN` using your secret store.
Keep the API's token read-only. The writer must be restricted to the chosen physical
index and its create, bulk index, metadata and search operations.

```powershell
uv run --no-sync python scripts/index_opensearch.py --subject YOUR_ADMIN_SUBJECT --borrower YOUR_BORROWER_ID --effective-at 2026-09-01 --create-index
```

Use `--create-index` only for initial provisioning. Omit it for subsequent scopes and
updates. The command verifies the current administrator and borrower grants before
remote operations. It writes bounded batches, checks each operation's acknowledgment,
waits for search visibility and verifies the complete authorized scope. Retrying an
interrupted publication replaces the same document IDs. It never deletes an index or
initializes a SQL catalog. Synchronize Weaviate with its [indexing command](../../docs/weaviate.md)
after canonical publication as well.

The index is a rebuildable projection. PostgreSQL remains authoritative, including
when a revoked row still exists in OpenSearch. Keep source backups and canonical
database recovery separate from index rebuilding.

## Local adapter exercise

This isolated setup exercises the standalone OpenSearch adapter with synthetic data.
The authenticated server uses the governed configuration above.

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
scope limits. Socket timeouts do not forcibly cancel an entire application workflow; configure service authentication for nonlocal access. [Search API
contract](https://docs.opensearch.org/latest/api-reference/search-apis/search/).

`HybridProvider` composes two explicitly configured providers that expose the exact
same authoritative `EvidenceCatalog` instance. Equal revision integers from distinct
catalogs do not satisfy this contract; replacing either catalog later is rejected. It
requests up to 100 candidates per branch and fuses ranks with RRF k=60. Both branches
must succeed under the same request, principal and catalog revision. Conflicting
canonical chunks fail the result. Verification and citation access retain both branch
freshness checks. Provider labels identify each configured branch.

The integration CLI writes its initial manifest before network access. Connection,
indexing, query and cleanup failures retain failed manifests and any collected
request/response records. Input, mapping, script and source hashes are checked again
at completion; changed inputs invalidate the run.
