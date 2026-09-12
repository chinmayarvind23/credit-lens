# CreditLens Performance, Capacity, and Cost

## Measured response-cache comparison

With disk-backed SQLite audit and local telemetry enabled in both modes, 150
measured serial loopback HTTP requests per mode produced p95 35.78 ms uncached and
29.21 ms warm: an 18.35% reduction. Three blocks alternate mode order. All 330
requests including warmups had distinct acknowledged audits and equal substantive
packets. The workload covers five fictional borrowers with lexical retrieval.
The committed-code gate repeat measured 23.14 ms uncached and 22.67 ms cached,
only 2.04% lower. Both runs are retained; the variability prevents treating the
first 18.35% as a stable performance improvement.
Cold-start/model loading and production/cloud latency are outside this measurement.

Reproduce with `python -m scripts.benchmark_response_cache --output FRESH_PRIVATE_DIR`.
The report records raw packets, timings, hashes, audits, traces and metrics. CI
rejects changed answers, incorrect hit state, missing audits, source drift or a
cached HTTP p95 above the separately declared 2.7-second budget. That ceiling is
not the observed result. No paid calls occurred; infrastructure cost/request is
unknown and is not reported as zero or a cloud saving.

## Modeled workload

- 50 registered users,
- 10 concurrent users,
- 1,250 queries/day.

This does not justify early microservices or Kubernetes.

## Target request budget

Illustrative target pre-measurement:

| Stage                   |   Target |
| ----------------------- | -------: |
| Auth                    |    30 ms |
| Query classification    |    40 ms |
| Query rewrite when used |   250 ms |
| Retrieval               |   350 ms |
| Reranking               |   250 ms |
| Deterministic tools     |   120 ms |
| Generation              | 1,300 ms |
| Citation validation     |   150 ms |
| Network/other           |   210 ms |
| Total p95 target        | 2,700 ms |

This is a budget, not a measured result.

## Experiments

Benchmark top-k, rerank candidate count, context token budget, cache strategy, query rewrite use, reranking on/off, HNSW `efSearch`, and ingestion/embedding batching.

## Load tests

Suggested concurrency:

- 10,
- 25,
- 50,
- 100.

Record throughput, p50, p95, p99, error rate, saturation, dependency behavior, and cost.

## Cost

Track LLM tokens, embedding cost, Snowflake/search cost where measurable, AWS compute, cache/database cost, and logging cost.

Unmeasured target values:

- average cost/request: $0.043,
- 31% reduction from measured baseline.

## Scale path

1. optimize measured bottleneck,
2. vertical scale,
3. cache repeated expensive work,
4. move expensive ingestion to SQS workers,
5. increase stateless ECS tasks behind ALB,
6. optimize database/read paths,
7. extract services only when independent scale or operational needs justify it.
