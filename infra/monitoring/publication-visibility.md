# SQL publication to complete input search visibility

This command checks a fixed physical-page fixture through `SqlEvidenceCatalog` and OpenSearch. It is an operator-run diagnostic, separate from application indexing.

Start only the owned cached fixtures:

```powershell
docker compose -p creditlens-publication -f infra/monitoring/publication-compose.yml up -d --pull never
$env:PYTHONPATH='src'
.venv\Scripts\python.exe -m scripts.measure_publication --pages C:\private\corpus\pages.jsonl --output ../resources/credit_lens/evidence/new-publication-run
docker compose -p creditlens-publication -f infra/monitoring/publication-compose.yml down
```

Wait for PostgreSQL on loopback 15434 and OpenSearch on loopback 19202 before
running the command. The dedicated containers pin already cached PostgreSQL
16.4-alpine and OpenSearch 3.7.0 digests. PostgreSQL has one CPU and 512 MiB;
OpenSearch has two CPUs, 1.5 GiB and a 512 MiB Java heap. These are disposable
synthetic fixtures, not production versions or security settings. No paid service
or download is needed. The command rejects a corpus other than the exact frozen
`pages.jsonl` SHA-256 recorded in `FROZEN_PAGES_SHA256`.

All physical pages are published in one SQL transaction through bounded catalog
method batches. The monotonic clock is captured immediately after commit returns.
SQL publication duration is separate. A committed canonical-row scan verifies
all chunk payloads against the frozen source and retains their identity hashes.
This is an owned index-writer scan covering every tenant, date version and ACL in
the fixture; it is not a user query or a change to access controls.

Every bulk operation must acknowledge its exact canonical ID. The script then
polls `_search` in groups of 100 expected IDs, checks complete shards, and compares
the full indexed metadata and exact searchable text with SQL canonical data.
One complete sweep must verify every expected chunk. Missing chunks from separate
sweeps are never combined into a false complete observation. An altered, duplicate
or foreign hit fails the run. No forced refresh or real-time document GET is used.

The 30-second visibility budget starts after SQL commit and includes canonical
readback, serialization, HTTP upload, refresh, all verification requests and client
scheduling. Individual HTTP calls have a five-second timeout; the overall budget
is checked between calls and does not forcibly cancel an in-flight socket. The
result is a client-observed upper bound for an isolated static fixture. It is not
a production SLA, steady-state percentile, live replication guarantee, or measure
of upstream OCR/embedding/queue time.

Private output contains the report, complete canonical ID manifest, bulk responses,
search responses, per-sweep coverage, LF-only metric bytes and a manifest hashing
all artifacts and relevant source/input files. Failed or partial observations
omit a numeric complete-visibility delay. Fixed metrics are:

- `creditlens_publication_drill_success`
- `creditlens_publication_expected_chunks`
- `creditlens_publication_verified_chunks`
- `creditlens_publication_all_input_visibility_seconds` when completely observed

These are unlabeled snapshot gauges. There are no identity or query labels.
The existing verified snapshot server can expose `metrics.prom` using its manifest
hash. A fresh random catalog/index is removed in finalization, and cleanup status
is retained even on failure. Always remove the dedicated containers with the
compose command after the drill; prior evidence directories remain untouched.

Contracts follow
[PostgreSQL transaction visibility](https://www.postgresql.org/docs/16/transaction-iso.html)
and [OpenSearch search results](https://docs.opensearch.org/latest/api-reference/search-apis/search/).
