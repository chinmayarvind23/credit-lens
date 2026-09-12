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

Check policy rule, borrower evidence, calculation, exception, missing documents, policy disposition,
next actions, and citations.

Use deterministic validators where exact answers exist.

Use DeepEval and RAGAS for semantic quality metrics.

Use an LLM judge only where judgment is required. Version its rubric.

The [isolated DeepEval runner](../infra/evaluation/README.md) now executes actual source-grounded
FaithfulnessMetric judgments against a digest-pinned local model. Its first two-control screen
failed: the judge scored a supported answer 0.5 after incorrectly marking a matching policy minimum
as contradictory. The failed run is retained. A second judge also failed; Qwen3 8B then passed
twelve frozen V3 controls covering amounts, entities, explicit dates, missing evidence, currency and
authority. Passing authored controls does not establish a semantic quality baseline. Saved packet
pilots verify evidence against canonical page spans and authored scope before judging. RAGAS is also
implemented, with calibration and field pilots documented below. The later full-population diagnostics are now reconciled. Independent human calibration
and validated semantic-quality gates remain open.

The initial six-packet JSON pilot stopped on its second case when generation was incomplete. Its
first score of 1.0 omitted the calculated DSCR from extracted claims. A separate two-metric pilot
retained both values but returned zero scores with explanations that confirmed the calculations.
Both runs are preserved as judge-validation failures. No full-packet groundedness or
unsupported-claim rate can be inferred from them, and verdicts are not repaired to agree with
expectations.

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

Compare NumPy exact cosine, FAISS, dense provider, OpenSearch BM25, Weaviate HNSW, hybrid fusion,
and cross-encoder reranking.

Measure Recall@K, MRR, nDCG, latency, indexing time, memory, and cost where applicable.

## Context-budget experiment

Sweep retrieval candidate count, rerank candidate count, final context chunks, and final context
tokens.

Measure grounded quality, citation quality, prompt tokens, p95 latency, and cost/request.

## Reproducibility manifest

Each run records metric status, git SHA, dataset version, corpus hash, chunker version, embedding
model/version, retrieval config, reranker, prompt version, generator model, seed, environment,
timestamp, and metrics.

## CI gates

Use a smoke eval on ordinary PRs.

Use a full evaluation before and after major retrieval, model, embedding, chunker, or index changes.

Hard security invariants cannot regress.

## Composed local model runtime

The clean `76aaa91` runtime experiment used the same frozen 3,840 pages and 240 authored cases as
the earlier dense/hybrid study. It reproduced every prior top-ten chunk ordering, yielding Recall@10
0.8254545454545454 and nDCG@10 0.7290997986576191 across 220 eligible cases. Micro recall was
337/465 (0.7247311827956989); 20 no-positive-qrel cases remain excluded from ranking averages. All
240 deterministic workflow fixture checks passed, with zero unauthorized context and zero path
failures. These remain exposed development fixtures, not independent semantic answer grades.

`scripts/benchmark_neural.py` exercises the configured serving provider, including current SQL
grants, scoped dense scoring, both fusion branches, cross-encoder, canonical result checks and
subsequent workflow/audit execution. Model loading took 8.70 seconds in this run. Provider-ranking
p95 was 1190.05 ms and mean was 734.33 ms. Document vectors were cached within bounded retention
across cases; questions were not cached. Each workflow ran after its separate ranking and therefore
reused document vectors. These values are local provider measurements, not HTTP latency, a cloud
comparison or a request-cost improvement. Other projects were running on the same host; no
exclusive-machine timing is claimed.

The actual-model HTTP check is separate: five financial scenarios, exact source inspection, denied
borrower scope, unrelated-question abstention, model overload and grant revocation. It observed no
external socket connections. Five observed round trips are insufficient for a population latency
claim. The complete local suite passed 440 tests; core statement coverage was 2315/2350 (98.5106%),
with every critical module above its 95% gate.

Private raw evidence is under `resources/credit_lens/evals/neural-runtime-76aaa91` and
`resources/credit_lens/evidence/neural-runtime-*`. Source, model-manifest, gold and corpus hashes
remained unchanged. A separate root-run recomputation from raw labels confirmed metric arithmetic
and all 240 prior top-ten matches; it is not described as an independent agent review. The later
Linux CPU replay at `51a2c75` reproduced all 240 rankings and fixture outcomes. That verified model
container was subsequently deployed behind the public HF entry page and checked through desktop and
mobile browsers.

## Candidate-budget experiment

The offline `abc0cdb` experiment compared ten branch/rerank budgets on the same 240 frozen cases,
with 220 eligible for ranking metrics. It scored each authorized query/chunk pair once and
reconstructed each candidate prefix from saved branch rankings. A separate recomputation verified
all rankings and metrics; the existing 100-branch/40-rerank baseline matched exactly on all 240
cases.

| Branch candidates | Rerank candidates | Recall@10 | nDCG@10 | Relevant page hits |
| --- | --- | --- | --- | --- |
| 20 | 20 | 82.61% | .7283 | 337/465 |
| 100 | 40, current baseline | 82.55% | .7291 | 337/465 |
| 100 | 80 | 82.85% | .7306 | 339/465 |
| 100 | 100 | 82.70% | .7300 | 338/465 |

With 100 candidates, every labeled relevant page reached the reranking pool, but the final top ten
still missed many of them. Increasing the pool alone is insufficient. The serving default remains 40
pending a stronger quality/latency tradeoff. Shared pair scores do not measure per-variant latency
or request cost. The benchmark records actual experiment time, scored pairs and context bytes; it
does not claim model-token or cloud-cost measurements. No variant is promoted from these exposed
development fixtures alone.

Run `scripts/benchmark_candidates.py` with explicit `--gold`, `--pages`, `--models` and a fresh
private `--output` directory. Model loading uses verified local weights and socket connections are
denied. Raw scores, ten variant rankings, input/source hashes, package versions and the separate
recomputation are retained in `resources/credit_lens/evals/candidate-budgets-abc0cdb`.

## Borrower-grounded query experiment

At `94db2cb`, the offline experiment prefixed questions with a single authorized application name
while preserving the original question. Lexical, dense and cross-encoder stages used the same
expanded question; models and candidate budgets stayed fixed. Name lookup did not broaden tenant,
borrower, ACL or effective-date scope. Missing, conflicting, malformed or oversized metadata
disabled augmentation.

| Variant | Recall@10 | nDCG@10 | Relevant page hits |
| --- | --- | --- | --- |
| Original question | 82.55% | .7291 | 337/465 |
| Name prefix on every eligible question | 78.02% | .6360 | 350/465 |
| Initial selective prefix | 84.72% | .7686 | 356/465 |
| Stricter selector, saved-output replay | 86.54% | .7878 | 360/465 |

Universal augmentation reduced macro recall despite increasing total hits because it lost
single-page policy cases while recovering some multi-page evidence. The initial selector improved
missing-document and contradiction retrieval but lost four general annual-review policy questions.
At `9573d00`, calendar frequency was removed as a standalone borrower-context cue. A replay changed
only which saved ranking was selected, with zero new model calls. It recovered those four cases and
had no per-case recall regressions across the 220 eligible cases relative to the original baseline.
All 240 cases remained in the audit.

This final adjustment used observed development results. It is not held-out validation, and the
exposed authored benchmark limits generalization claims. Separate raw-label recomputation verified
baseline identity, source scope, question preservation and metric arithmetic. Seven focused selector
tests pass. At `77a8957`, the same selector was integrated into the hybrid serving path. The
composed runtime reproduced all 240 selector rankings, 86.54% Recall@10, .7878 nDCG and 360/465
relevant-page hits. All 240 authored workflow checks passed, with no observed scope or path
failures. Source and input hashes remained stable. This serving replay does not convert
development-set tuning into held-out evidence.

The integration passed 392 main tests, 79 actual PostgreSQL/Redis/ElasticMQ tests and one additional
original-audit-identity test. Core statement coverage was 2388/2424 (98.51%), with every critical
module above 95%. Real-model HTTP checks exercised scoped queries, all five financial scenarios,
citations, overload and revocation with no observed external connections. Shared-catalog tests also
invalidated a result when another reader revoked the application page.

The provider wraps the existing rankers and preserves the original request for workflow checks,
audits and cache identity. Its internal source result retains the transformed model request;
verification reconstructs that transformation under current grants and catalog revision. The
grounding version participates in cache keys. The corrected Linux package at `d09b713` passes five
financial HTTP scenarios with exact source inspection, denied-scope and abstention checks. Seven
desktop/mobile browser checks pass against the local candidate, with a recording and audit evidence
confirming the grounding provider. This is a smoke test, not a Linux replay of the full benchmark.
At that historical stage, public promotion was pending. The current free release uses the lexical
Pyodide workflow, separate from this CPU experiment.

The first image failed startup because the staging inventory included the new module but
`.dockerignore` excluded it. The corrected whitelist and deployment inventory guard pass 15
packaging tests. The guard compares exact file exceptions; it does not interpret every Docker
pattern or replace actual container checks.

Run the model experiment with `python -m scripts.benchmark_grounding`, passing `--gold`, `--pages`,
`--models`, the verified candidate run as `--baseline` and a fresh private `--output` directory. Raw
V1 outputs are preserved in `resources/credit_lens/evals/query-grounding-94db2cb`; the V2 selector
replay is in `resources/credit_lens/evals/query-grounding-selector-9573d00`.

## RAGAS calibration

At `da7b751`, RAGAS 0.4.3 was integrated into the isolated local evaluation environment using the
same digest-pinned Qwen3 model and transport bounds. Four RAGAS protocol tests and ten existing
DeepEval transport tests pass. The actual model passed all twelve frozen V3 controls. The 24 raw
responses match the retained extraction and verdict outputs, with stable input/source hashes.

The two saved DSCR claims used exactly the same input bytes as the earlier DeepEval pilot. RAGAS
returned .5 for the 1.5000 claim and 0 for the 1.1000 claim. Both explanations computed the correct
ratio, then rejected it because the value was not explicitly written in the source. The first
extraction also added a DSCR definition absent from the original answer. All four raw responses are
retained. The process completed with stable provenance; judge validation failed.

The wrapper checks coverage of extracted statements by verdicts. It cannot prove that extraction
faithfully represents the original answer, as this pilot shows. These scores do not establish packet
groundedness, citation precision or an unsupported-claim rate. Arithmetic remains independently
checked with Decimal. Further calibration must test supported derivation, wrong calculations and
extraction fidelity before a population semantic run.

The separate `lending-v1` prompt at `4d5d5f6` was calibrated against those observed failures. Twelve
new financial controls cover supported derivation and rounding, wrong values, missing
formula/inputs, borrower/period/currency mismatches, zero denominators and unsupported approval. All
expected scores and exact atomic extractions passed. The original twelve V3 controls also passed
under this profile. These are exposed authored controls, not held-out validation.

Both unchanged saved DSCR claims now score 1.0, preserve the original sentence verbatim and include
the correct division in their reasons. The full JSON packet pilot still fails coverage: its first
score of 1.0 omits the displayed DSCR, and the second case stops on incomplete generation. Four
selected packets remain unrun. Source/input provenance is stable in every run. Prior default-prompt
failures remain preserved; no verdict or label was repaired. Evaluation needs an explicit field
inventory and bounded claim units before whole-packet scoring.

## Explicit packet field coverage

The exporter at `64dc55c` verifies saved packets against canonical spans and authored scope,
validates each citation tuple and exports every cited claim and metric by field path. Context is
restricted to the field's cited spans. Wrong claim text with a valid source identity stays
evaluable; export does not filter unsupported answers out of the denominator. All 19 Packet fields
are accounted for, with empty arrays, metadata and pending rubrics represented explicitly. Six
adversarial exporter tests pass.

The same six packets yield 85 units. Before judging, the pilot selected the first cited claim in
each packet and all three financial metrics, for nine units total. All nine score 1.0 with stable
provenance under the lending-v1 profile. Each metric is extracted verbatim, including the DSCR
omitted by whole-JSON extraction. All 18 raw responses match their retained parsed outputs. The
coverage ledger still contains 76 unselected units and 26 nonempty field entries awaiting rubrics.

These are selected-unit faithfulness scores, with a different denominator from the earlier
whole-packet pilots. They establish neither complete extraction within all fields nor question
relevance, whole-packet pass rate or population groundedness.

## Policy-reference field checks

Field review exposed false abstention on policy-date lookups: the application required borrower
financial inputs despite having policy evidence. The historical grounded fixture rubric accepts
evidence or abstention, so it did not detect the wrong state. That rubric and the original gold set
remain unchanged.

At `00cb2da`, a separate frozen contract covers ten threshold-reference questions and ten
policy-date questions. It requires cited policy, no abstention, no missing borrower documents, no
calculated metrics and the existing nonnumeric review state. The saved baseline passes 6/20; the
revised composed-model run passes 20/20. All 240 retrieval rankings and score objects match the
prior baseline, and all 240 historical fixture checks still pass with stable source/input
provenance. This is operational field validation, not semantic groundedness or human review.

## Verbatim field support

The expanded extraction-based pilot completed 57 of 85 cited units. All received 1.0, but source
inspection found four false positives: extraction interpreted a document-version header as a
directory location and NLI accepted the invented claim. That claim was absent from the application's
quoted answer. The two unstarted batches, containing 28 units, were paused and all prior scores
retained.

At `78588ba`, a separate custom RAGAS metric replaces generative extraction with one unchanged
complete field. The pinned library still constructs the NLI prompt and computes its score. A single
binary verdict must cover the exact original text; missing clauses, rewritten text and nonbinary
outputs fail validation. This changes the denominator from generated atomic claims to whole fields.

Twelve new compound/metadata controls and twelve original controls pass. All 85 cited fields in the
same six historical Linux packets then receive supported verdicts. The verifier checks every
original field and all 109 raw model outputs, with stable input/source provenance. JSON type
preservation prevents a boolean from masquerading as an integer verdict during reconciliation. Two
new metric protocol tests, six accounting tests and six existing RAGAS tests pass.

This completes cited-field coverage for this selected sample only. Its 26 nonempty
operational/advice/question fields still need suitable rubrics. This historical sample did not
establish whole-packet quality, question relevance, semantic citation applicability or human
calibration. Later packet grading is reported separately below. The result does not establish a
population groundedness rate or the requested quality gain.


## Population semantic reconciliation

The full saved lexical benchmark now exports all 240 cases. Its 235 packets contain 2,965 cited
field occurrences; five authorization denials remain in the case ledger. The local RAGAS lending
field-support run uses 441 byte-identical NLI prompt groups. Repeated fields retain their weight but
are not independent model judgments. The terminal run and independent raw reconciliation completed:
440/441 prompt groups and 2,963/2,965 field occurrences received supported verdicts (99.9325%
occurrence-weighted field support). No calls failed or remained ungraded. This is a diagnostic model
score, not a project accuracy estimate.

`scripts/reconcile_population.py` runs in the isolated semantic environment and makes no network or
model calls. It reconstructs the pinned library prompts, verifies every alias against the frozen
field inventory, compares completed verdicts to raw journals, checks model identity and generation
completion, and recomputes counts. Modified evidence or mismatched scores fail verification.

```powershell
<semantic-python> scripts/reconcile_population.py --population <frozen-population-directory> --run <population-results-directory> --output <new-private-report.json> --require-complete
```

The final gate requires terminal completion, stable evaluator/input provenance and every field
graded. A live snapshot preserves an acknowledged result prefix; only append-only progress is
allowed afterward. Previously observed journals, input fields and source code must remain unchanged.
Live snapshots have no final field-support rate and cannot pass `--require-complete`.

Even a complete field-support report does not establish whole-packet groundedness, question
relevance, human calibration or semantic citation precision. Uncited advice and other fields without
a semantic rubric remain visible in the case ledger. Requested project metrics must not be filled
with this narrower score.


The sole rejected prompt applies to DSCR fields in cases 107 and 137. Its cited inputs are 209600.00
cash flow and 135200.00 annual debt service; Decimal division is 1.550295857988165680473372781,
rounding to the emitted 1.5503. The judge instead claimed the quotient was 1.5517. The raw zero
verdict remains in the result; the arithmetic false negative is recorded separately. Positive
judgments have not been independently human calibrated.

A fresh full benchmark at clean commit `5b37642` passed all 240 fixture outcomes. All 2,965 complete
ordered semantic input dictionaries, all 235 substantive packets and all five denial outcomes match
the earlier saved run. Prior raw judgments are therefore explicitly reused for identical inputs, not
represented as newly generated results. Original dirty-source provenance remains retained. Volatile
request IDs, stages, provider/corpus execution metadata, timing, cache status and cost fields were
excluded only from packet equality, not semantic inputs.

The ledger retains 970 nonempty fields outside this rubric: 235 each of policy disposition, next
actions, underwriter questions and abstention, plus 30 missing-document fields. Local qwen3:8b ran
the 441 prompts through pinned RAGAS 0.4.3. Aggregate per-call HTTP time was 1,924.37 seconds; total
process wall time was not recorded. No paid inference was used. Full evidence is retained privately
in `evals/population-lexical-v2-final.json` (SHA-256
`8a38a33e9a43cbfe54f95a88687e58d0d4966b0bf6022fde73044aabc50d5d62`) and
`evals/population-clean-5b37642-equivalence.json`.

## Completed whole-packet v2 diagnostic

The final offline reconciliation accounts for the entire 240-case ledger: 235 packets graded and
five authorization denials. DeepEval GEval under the versioned whole-packet-lending-v2 rubric
returned 102/235 raw passes (43.4%), with 8/8 frozen controls. Both `human_calibrated` and
`validated_project_quality` are false. The rate is a raw diagnostic, not reliable accuracy or a
resume quality measurement. The prior v1 and smaller-model screens remain retained.

The first attempt completed 192 packet judgments and all controls. The final two batches failed
before output creation because the shared field-support reader rejected empty context lists. A
separately hashed recovery adapter accepted those unchanged empty lists, retained every completed
verdict and graded the remaining 43 cases. It changed no case, rubric, expected label or prompt
content. The original failed summary and adapter are frozen in the derived run's provenance. Offline
reconciliation verified all actual prompts, raw responses, identities and hashes.

An agent review of 12 deterministically selected cases found likely judge false negatives, including
quoted answer material treated as absent, compatible annual inputs treated as misaligned and an
overlooked MEETS_POLICY field. This review is not independent human calibration, does not replace
scores and cannot estimate error prevalence. Genuine relevance/completeness gaps elsewhere remain
possible. The field-support and packet rates measure different targets and must stay separate.

Private records: `evals/deepeval-whole-v2-final.json`, `audit/deepeval-v2-sample-review.md`,
`audit/packet-recovery-adapter-review.md` and ADR 051. Reproduction and the explicit recovery path
are described in the [judge runner guide](../infra/evaluation/README.md).

Actual RAGAS journals report 538,444 prompt and 65,932 completion tokens. Evaluation dashboards
import reconciled diagnostic artifacts; their presence does not validate a model's judgments.
Runtime cost metrics distinguish known and unknown values, excluding unknowns from dollar
histograms. No cloud dollar savings are inferred.

The final GEval raw journals contain 243 calls including eight controls and report 1,200,982 prompt
and 12,690 completion tokens. Actual Grafana proxy checks verified the two whole-packet query
expressions. This verifies the data path, not the judge's semantic correctness. Full results are
retained privately in `evidence/deepeval-whole-v2-results.md`.


A later 30-case agent review used three frozen packets per category and retained
27 passes and three failures after two agent-adjudicated relevance cases. The raw
DeepEval judge passed 13 of those same 30 packets; agreement was 16/30. This is an
agent diagnostic sample, not human calibration, a representative population rate,
or a replacement for the historical 102/235 DeepEval score. Original labels and
adjudication notes remain separate. The three observed failures exposed missing
refusals for unsupported scope/hidden-topic requests; their code fix has separate
regression evidence and does not retroactively change frozen outputs or scores.
