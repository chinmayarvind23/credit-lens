# ADR-003: Hybrid Retrieval with Rank Fusion and Reranking

## Status

Accepted as target architecture, subject to benchmark evidence.

## Decision

Benchmark dense retrieval plus BM25, fuse with RRF as the default baseline, then apply a cross-encoder reranker.

## Why RRF

BM25 and embedding scores are not naturally calibrated to the same scale. RRF uses rank positions.

## Tradeoff

RRF discards score magnitude. The reranker supplies fine-grained relevance.

## Switching condition

Adopt calibrated or learned fusion only if reproducible evals show enough gain to justify complexity.
