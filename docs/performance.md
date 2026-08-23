# CreditLens Performance, Capacity, and Cost

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
