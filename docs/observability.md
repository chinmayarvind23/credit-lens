# Observability

Install the `observability` extra and enable `CREDITLENS_TELEMETRY_ENABLED`. `CREDITLENS_TRACE_FILE` selects a private rotating JSONL trace destination. Each application owns its OTel provider and Prometheus registry.

Request spans correlate scoped retrieval, cache handling, calculations, citation checks, permission rechecks and audit persistence. The exporter excludes question text, source text, identity labels, credentials and exception payloads. Protected SQL audit records retain the application evidence trail.

The admin-only `/api/v1/metrics` endpoint exposes request, workflow, cache, disposition and ingestion signals. Labels use bounded route, stage and state values. Storage failures fail the ingestion scrape rather than reporting stale gauges.

The [local monitoring stack](../infra/monitoring/README.md) provides Prometheus, Grafana and alert rules. Semantic snapshots are imported separately from request telemetry. Token and cost signals distinguish known from unknown values. Indexing tools distinguish publication acknowledgment from searchable canonical content, while neural instrumentation records embedding and reranking operations.

The public browser shows its local execution trace and does not send server telemetry. Keep administrative scrape credentials outside the repository.
