# Local indexing measurements

This command measures real OpenSearch bulk writes and automatic-refresh canary
visibility on synthetic documents. It does not add a production indexer or a live
metrics exporter. Existing ingestion queue state age remains separate from index
lag.

Use the already cached pinned OpenSearch image with the isolated container:

```sh
docker compose -p creditlens-indexing-monitor -f infra/monitoring/indexing-compose.yml up -d --pull never
# After the local service on 127.0.0.1:19201 is ready:
PYTHONPATH=src python -m scripts.measure_indexing --pages /private/corpus/pages.jsonl --output ../resources/credit_lens/evidence/new-indexing-drill
docker compose -p creditlens-indexing-monitor -f infra/monitoring/indexing-compose.yml down
```

The container has two CPUs, 1.5 GiB memory and a 512 MiB Java heap. It binds only to
loopback, contains synthetic data and disables OpenSearch authentication. No cloud
resources, downloads or external account writes are needed. The command creates
one random index and deletes it in a finally block. Remove the dedicated container
after the command, including failed runs; cleanup status stays in the report.

The private output includes `report.json`, per-batch `bulk.jsonl`, `metrics.prom`
and `manifest.json`. The manifest binds the metric bytes through `metrics_sha256`
and hashes the report, bulk records, input, mapping and measurement code. Source
changes during a run fail the evidence status. The generic `serve_snapshot.py`
can serve this verified metric file for local monitoring.
Metrics are written as UTF-8 bytes with LF endings on every platform; Windows
text-mode CRLF is rejected by Prometheus's numeric parser.

| Metric | Meaning |
|---|---|
| `creditlens_indexing_run_duration_seconds` | Client elapsed upload time, including serialization, HTTP and response validation |
| `creditlens_indexing_run_units_per_second` | Fully acknowledged input chunks/pages/document versions divided by upload time |
| `creditlens_indexing_run_chunks` | Exact item acknowledgments, rejections or unconfirmed operations |
| `creditlens_indexing_run_bulk_failures` | Batches containing rejected/unconfirmed operations |
| `creditlens_indexing_canary_visible` | Whether the actual search canary became visible inside the observation budget |
| `creditlens_indexing_canary_visibility_seconds` | Client acknowledgment processing to first successful search observation; omitted on failure |

All values are gauges in a completed drill snapshot, not lifetime counters.
Labels are finite phase (`corpus`, `fault`), unit (`chunks`, `pages`, `documents`)
and item state. There are no query, borrower, tenant, document or index labels.
The fault phase deliberately submits one unmapped field to the strict index and
checks its rejection. It is excluded from corpus throughput.

Acknowledgment does not establish search visibility. Page/document throughput
requires all of that input unit's chunks to be acknowledged, but does not prove
the input contains every page of the original source document. Canary visibility
is a client-observed upper bound with 100 ms polling, HTTP and scheduling overhead;
one canary is not corpus-wide freshness. The five-second observation budget and
five-second HTTP timeout are checked between requests and do not cancel a socket
mid-request. There is no forced refresh.

The canary also checks exact canonical citation metadata and current authorization:
after revocation in the memory fixture, it remains visible to raw search but is
absent from authorized provider results. This is a permission canary, not a SQL
durability test. Embeddings do not run here. Use the separate publication-visibility command for the SQL-to-search interval. Queue age is a different instrument.

Contracts follow [OpenSearch bulk item results](https://docs.opensearch.org/latest/api-reference/document-apis/bulk/)
and [refresh behavior](https://docs.opensearch.org/latest/api-reference/index-apis/refresh/).
