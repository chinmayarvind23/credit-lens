# CreditLens Observability

## Implemented local telemetry

Enable `CREDITLENS_TELEMETRY_ENABLED=true` after installing the `observability`
extra. Each app owns an OpenTelemetry SDK provider and a Prometheus registry.
`CREDITLENS_TRACE_FILE` optionally selects a private JSONL file; rotation retains
at most three 5 MB files. There is no remote exporter or telemetry subscription.

HTTP root spans contain only a bounded route, method and response status. Workflow
spans cover current grants, scoped retrieval, cache hits, calculations, citations,
permission rechecks and audit persistence. Question text, documents, credentials,
borrower/tenant IDs and exception messages are excluded from the exporter. SQL
still owns the complete protected audit, including evidence identity.

Implemented metrics are `creditlens_requests_total` by finite route/method/status
class, `creditlens_request_duration_seconds` histogram by route, and
`creditlens_packets_total` by disposition/cache state. The HTTP metrics endpoint is
`/api/v1/metrics` and requires a current admin grant; it is disabled when telemetry
is off. A Prometheus operator can scrape it using their managed administrator access
token. Never place credentials in source or a public Space.

Real SDK integration checks establish stage/root trace correlation across FastAPI's
thread boundary, error status without exception text, cache-hit counters and denied
metrics access. Actual loopback HTTP runs record metrics and rotating trace files.
The free browser demo exposes its per-request stage trace, but does not load this
server-only SDK or send telemetry anywhere.

The broader signal catalog below remains a plan. LangSmith, CloudWatch, an OTLP
collector and managed Grafana dashboards are not deployed.


## Goals

Answer:

1. Why did this request fail or become slow?
2. How is the system behaving over many requests?

Use traces for the first and aggregate metrics for the second.

## Trace structure

```text
creditlens.query
  auth.validate
  query.classify
  query.rewrite
  retrieval.cortex
  retrieval.lexical
  retrieval.rerank
  finance.calculate
  llm.generate
  citation.verify
  policy.validate
```

## Trace attributes

Where safe:

- request ID,
- pseudonymous tenant key,
- borrower safe identifier,
- model,
- prompt version,
- embedding version,
- index version,
- retrieval strategy,
- top-k,
- retrieved chunk IDs,
- rerank scores,
- cache hit,
- token counts,
- stage latency,
- total latency,
- cost estimate,
- abstention,
- citation validation.

## Prometheus metrics

- `creditlens_requests_total`
- `creditlens_request_duration_seconds`
- `creditlens_retrieval_duration_seconds`
- `creditlens_rerank_duration_seconds`
- `creditlens_generation_duration_seconds`
- `creditlens_tokens_total`
- `creditlens_cost_usd_total`
- `creditlens_cache_hits_total`
- `creditlens_cache_misses_total`
- `creditlens_abstentions_total`
- `creditlens_citation_failures_total`
- `creditlens_acl_denials_total`
- `creditlens_index_lag_seconds`
- `creditlens_ocr_failures_total`
- `creditlens_ingestion_jobs_total`

## Dashboards

API: volume, success rate, p50, p95, p99, errors.

Retrieval: scheduled eval quality, retrieval latency, rerank latency, top-k, rewrite rate.

AI quality: grounded pass rate, unsupported claims, citation failures, abstention, tokens/request, cost/request.

Ingestion: docs/hour, pages/hour, OCR failures, embedding failures, index lag.

Security: authorization denials and controlled adversarial test signals.

## Tool responsibilities

- OTel: vendor-neutral distributed telemetry.
- LangSmith: AI-specific trace and prompt/retrieval debugging.
- Prometheus/Grafana: aggregate operational dashboards.
- CloudWatch: AWS service/infrastructure telemetry.

Avoid duplicating every signal everywhere without a debugging reason.
