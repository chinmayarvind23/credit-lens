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

The [isolated DeepEval runner](../infra/evaluation/README.md) now executes actual
source-grounded FaithfulnessMetric judgments against a digest-pinned local model.
Its first two-control screen failed: the judge scored a supported answer 0.5 after
incorrectly marking a matching policy minimum as contradictory. The failed run
is retained. A second judge also failed; Qwen3 8B then passed twelve frozen V3
controls covering amounts, entities, explicit dates, missing evidence, currency
and authority. Passing authored controls does not establish a semantic quality
baseline. Saved packet pilots verify evidence against canonical page spans and
authored scope before judging. Human calibration, RAGAS, the full 240-case semantic
run and associated regression gates remain open.

The initial six-packet JSON pilot stopped on its second case when generation
was incomplete. Its first score of 1.0 omitted the calculated DSCR from extracted
claims. A separate two-metric pilot retained both values but returned zero scores
with explanations that confirmed the calculations. Both runs are preserved as
judge-validation failures. No full-packet groundedness or unsupported-claim rate
can be inferred from them, and verdicts are not repaired to agree with expectations.

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

| Category                     | Count |
| ---------------------------- | ----: |
| Direct policy lookup         |    60 |
| Borrower evidence            |    40 |
| Structured calculation       |    30 |
| Multi-document synthesis     |    25 |
| Missing documents            |    20 |
| Policy exceptions            |    20 |
| Contradictory evidence       |    15 |
| ACL / tenant isolation       |    10 |
| Stale/versioned policy       |    10 |
| Prompt injection/adversarial |    10 |
| Total                        |   240 |

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

Use a full evaluation before and after major retrieval, model, embedding, chunker, or index changes.

Hard security invariants cannot regress.

## Composed local model runtime

The clean `76aaa91` runtime experiment used the same frozen 3,840 pages and 240
authored cases as the earlier dense/hybrid study. It reproduced every prior top-ten
chunk ordering, yielding Recall@10 0.8254545454545454 and nDCG@10
0.7290997986576191 across 220 eligible cases. Micro recall was 337/465
(0.7247311827956989); 20 no-positive-qrel cases remain excluded from ranking
averages. All 240 deterministic workflow fixture checks passed, with zero
unauthorized context and zero path failures. These remain exposed development
fixtures, not independent semantic answer grades.

`scripts/benchmark_neural.py` exercises the configured serving provider, including
current SQL grants, scoped dense scoring, both fusion branches, cross-encoder,
canonical result checks and subsequent workflow/audit execution. Model loading
took 8.70 seconds in this run. Provider-ranking p95 was 1190.05 ms and mean was
734.33 ms. Document vectors were cached within bounded retention across cases;
questions were not cached. Each workflow ran after its separate ranking and
therefore reused document vectors. These values are local provider measurements,
not HTTP latency, a cloud comparison or a request-cost improvement. Other projects
were running on the same host; no exclusive-machine timing is claimed.

The actual-model HTTP check is separate: five financial scenarios, exact source
inspection, denied borrower scope, unrelated-question abstention, model overload
and grant revocation. It observed no external socket connections. Five observed
round trips are insufficient for a population latency claim. The complete local
suite passed 440 tests; core statement coverage was 2315/2350 (98.5106%), with
every critical module above its 95% gate.

Private raw evidence is under `resources/credit_lens/evals/neural-runtime-76aaa91`
and `resources/credit_lens/evidence/neural-runtime-*`. Source, model-manifest,
gold and corpus hashes remained unchanged. A separate root-run recomputation
from raw labels confirmed metric arithmetic and all 240 prior top-ten matches;
it is not described as an independent agent review. The later Linux CPU replay
at `51a2c75` reproduced all 240 rankings and fixture outcomes. That verified
model container was subsequently deployed behind the public HF entry page and
checked through desktop and mobile browsers.

## Candidate-budget experiment

The offline `abc0cdb` experiment compared ten branch/rerank budgets on the same
240 frozen cases, with 220 eligible for ranking metrics. It scored each authorized
query/chunk pair once and reconstructed each candidate prefix from saved
branch rankings. A separate recomputation verified all rankings and metrics;
the existing 100-branch/40-rerank baseline matched exactly on all 240 cases.

| Branch candidates | Rerank candidates | Recall@10 | nDCG@10 | Relevant page hits |
| --- | --- | --- | --- | --- |
| 20 | 20 | 82.61% | .7283 | 337/465 |
| 100 | 40, current baseline | 82.55% | .7291 | 337/465 |
| 100 | 80 | 82.85% | .7306 | 339/465 |
| 100 | 100 | 82.70% | .7300 | 338/465 |

With 100 candidates, every labeled relevant page reached the reranking pool,
but the final top ten still missed many of them. Increasing the pool alone is
insufficient. The serving default remains 40 pending a stronger quality/latency
tradeoff. Shared pair scores do not measure per-variant latency or request cost.
The benchmark records actual experiment time, scored pairs and context bytes;
it does not claim model-token or cloud-cost measurements. No variant is promoted
from these exposed development fixtures alone.

Run `scripts/benchmark_candidates.py` with explicit `--gold`, `--pages`, `--models`
and a fresh private `--output` directory. Model loading uses verified local
weights and socket connections are denied. Raw scores, ten variant rankings,
input/source hashes, package versions and the separate recomputation are retained
in `resources/credit_lens/evals/candidate-budgets-abc0cdb`.

## Borrower-grounded query experiment

At `94db2cb`, the offline experiment prefixed questions with a single authorized
application name while preserving the original question. Lexical, dense and
cross-encoder stages used the same expanded question; models and candidate budgets
stayed fixed. Name lookup did not broaden tenant, borrower, ACL or effective-date
scope. Missing, conflicting, malformed or oversized metadata disabled augmentation.

| Variant | Recall@10 | nDCG@10 | Relevant page hits |
| --- | --- | --- | --- |
| Original question | 82.55% | .7291 | 337/465 |
| Name prefix on every eligible question | 78.02% | .6360 | 350/465 |
| Initial selective prefix | 84.72% | .7686 | 356/465 |
| Stricter selector, saved-output replay | 86.54% | .7878 | 360/465 |

Universal augmentation reduced macro recall despite increasing total hits because
it lost single-page policy cases while recovering some multi-page evidence. The
initial selector improved missing-document and contradiction retrieval but lost
four general annual-review policy questions. At `9573d00`, calendar frequency was
removed as a standalone borrower-context cue. A replay changed only which saved
ranking was selected, with zero new model calls. It recovered those four cases
and had no per-case recall regressions across the 220 eligible cases relative to
the original baseline. All 240 cases remained in the audit.

This final adjustment used observed development results. It is not held-out
validation, and the exposed authored benchmark limits generalization claims.
Separate raw-label recomputation verified baseline identity, source scope,
question preservation and metric arithmetic. Seven focused selector tests pass.
At `77a8957`, the same selector was integrated into the hybrid serving path.
The composed runtime reproduced all 240 selector rankings, 86.54% Recall@10,
.7878 nDCG and 360/465 relevant-page hits. All 240 authored workflow checks passed,
with no observed scope or path failures. Source and input hashes remained stable.
This serving replay does not convert development-set tuning into held-out evidence.

The integration passed 392 main tests, 79 actual PostgreSQL/Redis/ElasticMQ tests
and one additional original-audit-identity test. Core statement coverage was
2388/2424 (98.51%), with every critical module above 95%. Real-model HTTP checks
exercised scoped queries, all five financial scenarios, citations, overload and
revocation with no observed external connections. Shared-catalog tests also
invalidated a result when another reader revoked the application page.

The provider wraps the existing rankers and preserves the original request for
workflow checks, audits and cache identity. Its internal source result retains
the transformed model request; verification reconstructs that transformation
under current grants and catalog revision. The grounding version participates
in cache keys. Linux container validation and public deployment remain separate.

Run the model experiment with `python -m scripts.benchmark_grounding`, passing
`--gold`, `--pages`, `--models`, the verified candidate run as `--baseline` and a
fresh private `--output` directory. Raw V1 outputs are preserved in
`resources/credit_lens/evals/query-grounding-94db2cb`; the V2 selector replay is in
`resources/credit_lens/evals/query-grounding-selector-9573d00`.
