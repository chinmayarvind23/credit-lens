# CreditLens Observability

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
