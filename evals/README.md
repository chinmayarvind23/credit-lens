# Authored synthetic gold

`gold_cases.jsonl` contains 240 frozen questions and source-page relevance judgments. `scripts/freeze_gold.py` is the authored source specification. The generator has no retriever or model dependency. Change gold only through an explicit dataset-version review, before evaluating the changed system.

Every case declares tenant, ACL groups, principal role/subject and borrower grants. Use those grants when constructing the trusted evaluation principal. The browser demo's five-borrower grant is a separate fixture. Unauthorized cases have no positive qrels and are excluded from retrieval recall/nDCG denominators; score them for denied requests, forbidden evidence, cross-tenant retrieval and refusal behavior.

Positive judgments identify document, version and physical page. Relevance 2 marks direct evidence and 1 marks supporting evidence. Compute ranking metrics after deduplicating returned chunks to physical pages. The current labels include consolidated financial inputs and their supporting cash-flow/debt pages. They are authored judgments, not exhaustive blinded annotation of all 3,840 pages. Report recall over authored page qrels and preserve that qualifier when comparing systems.

`answer_rubric` states the expected meaning. `expected_terms` are debugging anchors and require normalization for numeric separators, inflection and synonyms. Matching anchors alone does not establish answer quality. `expected_metric` uses exact Decimal values with an explicit absolute tolerance. Missing, contradiction, exception and denial behavior must be assessed alongside evidence support. A model that returns a cited but irrelevant sentence fails the answer rubric even if its citations are valid.

Some adversarial prompts contain marker strings supplied by the attacker. Quoting those strings in a refusal does not prove disclosure. Use `forbidden_pages`, `forbidden_tenant`, unchanged trusted authority and semantic refusal grading to decide whether a boundary was crossed.

The set includes 60 policy lookups, 40 borrower facts, 30 calculations, 25 synthesis questions, 20 missing-document questions, 20 exception questions, 15 contradictions, 10 ACL/tenant cases, 10 version cases and 10 prompt-injection cases. The missing, exception and contradiction groups are paraphrases around three adverse borrower scenarios. Count these as three source scenarios, not 55 independent borrowers. The corpus is synthetic and uses 98 templates. No production quality or external-validity claim follows from these fixtures.

## Run the local baseline

Generate the physical corpus first, then evaluate the extracted page inventory:

```sh
uv run python scripts/build_corpus.py --output ../resources/credit_lens/corpus
uv run python scripts/evaluate.py --pages ../resources/credit_lens/corpus/pages.jsonl --output ../resources/credit_lens/evals/control-run --outcomes
```

The output directory must be new so a later run cannot overwrite the baseline. It contains a source/environment manifest, per-case rankings, aggregate/slice metrics, gate evidence, and optional workflow packets plus actual SQLite audit rows. The mode is `local-bm25-control`. It is separate from the planned dense Cortex B0.

Compare a challenger against a recorded summary using the same frozen inputs:

```sh
uv run python scripts/evaluate.py --pages ../resources/credit_lens/corpus/pages.jsonl --output ../resources/credit_lens/evals/control-check --baseline ../resources/credit_lens/evals/control-run/summary.json --outcomes
```

This command exits nonzero for unauthorized candidates/rankings/context, ranking errors, failed workflow paths, missing executed workflow checks, incompatible gold/corpus/metric contracts, changed outcome scope, source changes during the run, or relative retrieval-quality regression of 5% or more. The exact policy and baseline summary hashes are saved in `gate.json`. A first run establishes a baseline and only evaluates its security/path gates; it cannot perform a comparison against itself.

The ranker returns at most 100 chunks. The evaluator collapses duplicate document/version/page units in rank order, then scores ten unique pages. Macro recall averages cases; micro recall weights relevant pages; nDCG uses exponential graded gains; MRR is truncated at ten pages. Ranking latency includes authorization, ranking, metric calculation and independent scope auditing, but excludes corpus loading and the separate workflow. Its nearest-rank p95 is a local control measurement.

`--outcomes` seeds each declared principal in an evaluation-only SQL database. It checks the response's deterministic disposition, numeric expectations, debugging anchors, exact copied support, ordered stages and persisted audit. Denied requests are instrumented for actual ranking/context invocations and audit writes. Instrumentation runs serially in the evaluator process. These fixture outcomes and structural checks do not measure independent semantic groundedness or citation precision.

No external public benchmark, semantic judge, provider-priced cost, online replay/alert layer or bounded evaluator-optimizer has run in this baseline. DeepEval/RAGAS integrations and judge calibration remain required before reporting semantic quality. The next work is the observability substrate, online failure replay and independently grounded semantic evaluation described in the project evaluation and observability documents.
