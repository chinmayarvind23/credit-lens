# CreditLens Evaluation Design

## Principle

Each CreditLens eval reports separate:

1. **Outcome quality**
2. **Execution-path correctness**
3. **System performance**

Do not collapse them into one blended score.

## Retrieval metrics

### Recall@K

`Recall@K = relevant retrieved in top K / total relevant`

### Precision@K

`Precision@K = relevant retrieved in top K / K`

### Reciprocal rank

`RR = 1 / rank(first relevant result)`

### MRR

Mean reciprocal rank across cases.

### nDCG@K

Rewards relevant evidence appearing near the top and supports graded relevance.

## Generation metrics

- grounded-answer pass rate,
- unsupported-claim rate,
- answer relevance,
- citation precision,
- citation recall,
- abstention precision,
- abstention recall,
- calculation accuracy,
- exception F1,
- schema-valid response rate.

## Outcome grade

Check policy rule, borrower evidence, calculation, exception, missing documents, policy disposition, next actions, and citations.

Use deterministic validators where exact answers exist.

Use DeepEval and RAGAS for semantic quality metrics.

Use an LLM judge only where judgment is required. Version its rubric.

## Execution-path grade

Inspect the ordered event log.

Hard checks include:

- authentication occurred,
- authorization occurred before retrieval,
- tenant/borrower/ACL filters were applied,
- no unauthorized chunk entered retrieval,
- no unauthorized chunk entered model context,
- required deterministic calculator was used,
- cited chunk was retrieved,
- correct policy version was selected,
- context budget was respected,
- forbidden operations did not occur.

A correct final answer can still fail execution-path grading.

## 240-case suite

| Category | Count |
|---|---:|
| Direct policy lookup | 60 |
| Borrower evidence | 40 |
| Structured calculation | 30 |
| Multi-document synthesis | 25 |
| Missing documents | 20 |
| Policy exceptions | 20 |
| Contradictory evidence | 15 |
| ACL / tenant isolation | 10 |
| Stale/versioned policy | 10 |
| Prompt injection/adversarial | 10 |
| Total | 240 |

## Golden-case schema

```yaml
id:
question:

identity:
  tenant_id:
  user_id:
  role:
  borrower_id:

effective_at:

gold_outcome:
  expected_policy_rule:
  expected_calculation:
  expected_exception:
  expected_documents:
  expected_pages:
  expected_disposition:
  should_abstain:

expected_execution:
  required_steps:
  forbidden_events:
  allowed_document_ids:
  expected_policy_version:
  max_context_tokens:
  max_retrieval_k:

difficulty:
category:
```

## Baseline ladder

### B0

- fixed/token chunking,
- dense-only,
- top-5,
- no reranker.

### B1

- semantic/structure-aware chunking,
- dense-only.

### B2

- semantic chunks,
- dense + BM25.

### B3

- hybrid fusion,
- reranking.

### B4

- selective HyDE / follow-up rewrite.

### B5

- citation enforcement,
- abstention.

### B6

- caching,
- latency/cost optimization.

## Retrieval lab

Compare NumPy exact cosine, FAISS, dense provider, OpenSearch BM25, Weaviate HNSW, hybrid fusion, and cross-encoder reranking.

Measure Recall@K, MRR, nDCG, latency, indexing time, memory, and cost where applicable.

## Context-budget experiment

Sweep retrieval candidate count, rerank candidate count, final context chunks, and final context tokens.

Measure grounded quality, citation quality, prompt tokens, p95 latency, and cost/request.

## Reproducibility manifest

Each run records metric status, git SHA, dataset version, corpus hash, chunker version, embedding model/version, retrieval config, reranker, prompt version, generator model, seed, environment, timestamp, and metrics.

## CI gates

Use a smoke eval on ordinary PRs.

Use a full evaluation before release and after major retrieval, model, embedding, chunker, or index changes.

Hard security invariants cannot regress.
