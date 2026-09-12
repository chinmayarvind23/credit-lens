# Evaluation usage

The authored fixtures cover policy interpretation, borrower facts, calculations, missing evidence, contradictions, exceptions, access restrictions and effective dates. Gold labels identify physical document pages so chunking changes do not change the relevance unit. Keep the trusted evaluation principal separate from adversarial request text.

## Run

Generate a synthetic corpus and choose a new external output directory for each run:

```powershell
uv run python scripts/build_corpus.py --output ../resources/credit_lens/corpus
uv run python scripts/evaluate.py --pages ../resources/credit_lens/corpus/pages.jsonl --output ../resources/credit_lens/evals/control-run --outcomes
uv run python scripts/evaluate.py --pages ../resources/credit_lens/corpus/pages.jsonl --output ../resources/credit_lens/evals/control-check --baseline ../resources/credit_lens/evals/control-run/summary.json --outcomes
```

The harness preserves source and corpus identity, per-case rankings, scoped workflow packets and gate decisions. It refuses to overwrite an existing run. A baseline comparison requires compatible inputs and scoring contracts.

Retrieval scoring deduplicates chunks to physical pages. Unauthorized cases remain in the ledger and are checked for denial and forbidden evidence. Deterministic workflow checks cover dispositions, Decimal values, citation structure, stage order and audit persistence. These checks are separate from semantic judgments.

[Local semantic runners](../infra/evaluation/README.md) use DeepEval and RAGAS with retained prompts, controls and raw judgments. Preserve the rubric and ungraded cases when recovering an interrupted run. The executable gate policy is [gates.json](gates.json).
