# CreditLens Failure Modes

## RAG mistakes the system is designed to avoid

| Failure | CreditLens control |
|---|---|
| Treating RAG as prompt tuning | Retrieval-first benchmarks and ablations |
| No eval set | Versioned 240-case golden suite |
| Fixed-size chunking only | Fixed/token baseline plus semantic/structure-aware experiments |
| Weak indexing | Metadata, ACL, versions, hybrid search, migration plan |
| ACL after retrieval | Mandatory filters before retrieval |
| No caching | Versioned embedding/retrieval caches |
| Overfetching context | Context-budget experiments |
| Bad citations | Exact document/page/chunk provenance |
| No retries/timeouts | Per-dependency budgets, bounded retries, fallbacks |
| No observability | Stage traces, metrics, dashboards |

## Dependency failures

### Redis unavailable

Fallback to uncached behavior where safe.

### Snowflake/Cortex unavailable

Bounded retry. Do not fabricate evidence. Return explicit error or authorized fallback only if freshness/security contracts are satisfied.

### Reranker unavailable

Use documented fallback ranking and emit degraded telemetry.

### LLM unavailable

Return retryable error or evidence-only structured result.

### OCR failure

Mark extraction failed. Do not silently index low-confidence output.

### RDS unavailable

Do not acknowledge state changes that cannot be persisted durably.

### SQS backlog

Expose queue age/backlog. Scale workers or apply backpressure.

## Data failures

Missing documents, duplicate documents, stale/future policy, contradictory borrower values, corrupted PDFs, incorrect OCR, and inconsistent structured/document values.

## AI failures

Unsupported claims, citation mismatch, correct outcome through unsafe path, missed abstention, unnecessary abstention, prompt injection, context contamination, wrong rewrite, irrelevant reranking.

## Required behavior

Failure must be visible, typed, testable, and observable.

Do not turn uncertain evidence into confident prose.
