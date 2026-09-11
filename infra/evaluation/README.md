# Local semantic evaluation

This isolated environment uses DeepEval 4.2.2 without changing serving dependencies.
It has run actual FaithfulnessMetric judgments through an installed local model.
The first judge screen **failed**: Llama 3.1 8B Q4_K_M scored both a supported and
contradicted control 0.5, incorrectly calling the matching 1.25 policy minimum a
contradiction. Llama 3.2 3B also failed its supported control. Qwen3 8B subsequently
passed all twelve frozen V3 controls, scoring supported claims 1 and contradicted
claims 0. The V3 dates are explicit in the judged claims because Faithfulness does
not use the question to resolve dates. Earlier controls and failed runs are retained.
This clears authored screening only. RAGAS is also implemented, with calibration
and field pilots described below. Human calibration and the full 240-case semantic
baseline remain unfinished.

Actual saved-packet pilots exposed limits that the controls missed. A six-packet
JSON run stopped at its second case after incomplete generation; the first case
scored 1.0 while omitting its calculated DSCR from extracted claims. A separate
two-metric run extracted both saved values, then scored both zero despite reasons
that confirmed the calculations. The local judge is therefore not validated for
project quality claims. Preserve those raw failures and keep deterministic
arithmetic checks; do not repair judge verdicts or report a packet pass rate.

## Run the screen

Use an existing local Ollama endpoint at `127.0.0.1:11434`. The evaluator neither
starts that service nor pulls a model. Read `/api/tags` to select an installed
model's exact digest. It rejects cloud tags, remote descriptors and changed
digests. Do not replace the custom judge with a default hosted model.

```powershell
$env:UV_PROJECT_ENVIRONMENT=(Join-Path (Resolve-Path ..\resources\credit_lens) 'semantic-venv')
uv sync --project infra/evaluation --locked
..\resources\credit_lens\semantic-venv\Scripts\python.exe infra/evaluation/run_deepeval.py --cases infra/evaluation/controls.jsonl --model llama3.1:8b --digest 46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e --output ..\resources\credit_lens\evals\new-judge-screen
```

Use a fresh output directory outside the repository. The runner disables telemetry,
dotenv and legacy credential files and blocks socket connections except the
explicit local endpoint. This process restriction is not an OS sandbox for an
untrusted Ollama server; the local server must be trusted and use local weights.
Model checks before and after calls detect ordinary tag changes, not adversarial
server behavior. No shared service is stopped or reconfigured.

This initial screening runner accepts 1–24 cases, at most 100 model calls, 32 KB
per prompt, 1 MB per response, 8,192 context tokens and 2,048 generated tokens.
Temperature and seed are zero. It deliberately runs metrics sequentially. HTTP
timeouts are 120 seconds; a whole-process wall-clock supervisor is separate work.
Raw completed responses, generated claims/truths/verdicts, errors and source/input
hashes are saved privately. Incomplete generation or a failed control exits nonzero.

Each input row contains `id`, `input`, `actual_output`, `retrieval_context`, and
optionally `expected_range` for screening controls. Context must come from source
evidence, not another unsupported generated answer. The metric scores consistency
with that context; it does not establish source accuracy, citation applicability,
authorization, or arithmetic correctness. Keep existing deterministic checks.

Protocol tests use explicit transport doubles and do not measure judge quality:

```powershell
..\resources\credit_lens\semantic-venv\Scripts\python.exe -m unittest discover -s infra/evaluation -p test_local_judge.py -v
```

`export_packets.py` runs in the main project environment and exports explicitly
selected saved benchmark packets. It verifies retrieved spans and metadata against
canonical pages and audits authored tenant, borrower, ACL and effective-date scope.
It preserves factual packet fields and citations as JSON, excluding runtime metadata;
it does not rewrite an answer or regenerate retrieval. Missing or denied selected
packets fail export instead of disappearing from the denominator. A private manifest
records the selected IDs, original input hashes and exported bytes. Run a small pilot
before expanding; a selected subset cannot establish population quality.

## RAGAS local calibration

RAGAS 0.4.3 now uses the same pinned local transport through its structured-output
interface. `run_ragas.py` runs the library's unchanged Faithfulness prompts and
retains both extracted statements and NLI verdicts. Empty extraction, missing or
rewritten verdict statements, nonbinary values and inconsistent scores fail the
run. These checks validate score structure, not the judge's semantic correctness
or complete extraction of the original answer. Calibration remains required.

The actual Qwen3 run passed all twelve frozen V3 controls. The two saved DSCR
claims then scored .5 and 0 using inputs identical to the DeepEval pilot. Both
verdict explanations calculated the correct ratio but rejected it because the
derived value was not stated literally in the context. The first extraction also
added a definition absent from the answer. The run completed with stable
provenance, but judge validation failed. These are diagnostic pilot scores, not
project quality estimates. Neither framework is validated for population metrics.

The lock pins `langchain-community==0.4.1` because RAGAS imports VertexAI classes
removed in 0.4.2, even when the selected judge is local. No VertexAI client is
created. The runner disables telemetry before imports and restricts sockets to
the local Ollama endpoint. Its standard-library event loop is initialized first
to permit Windows's internal wakeup sockets. Cases run sequentially with at most
48 model calls under the existing transport bounds; there is no hosted fallback.

```powershell
..\resources\credit_lens\semantic-venv\Scripts\python.exe infra/evaluation/run_ragas.py --cases infra/evaluation/controls-v3.jsonl --model qwen3:8b --digest 500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41 --output ..\resources\credit_lens\evals\new-ragas-screen
..\resources\credit_lens\semantic-venv\Scripts\python.exe -m unittest discover -s infra/evaluation -p test_ragas.py -v
```

Use a fresh directory and an existing model with that verified digest. Raw prompts,
responses, extracted statements, verdicts, errors, input hashes and source/lock
hashes remain private. Protocol tests use explicit doubles and do not demonstrate
model quality. The full 240-case RAGAS semantic baseline has not been established.

An experimental `--profile lending-v1` adds explicit rules for supported
derivation and faithful extraction. The default `--profile stock` keeps the
library's original instructions. Each run records the profile and instruction
hashes. The experiment addresses observed pilot failures and is development
calibration, not held-out validation. `controls-derivation-v1.jsonl` contains
twelve frozen positive/negative financial controls with exact single-statement
extraction expectations. Rerun `controls-v3.jsonl` too before assessing the
unchanged saved calculations. Failed controls block promotion; do not reinterpret
their labels or replace earlier outputs.

At `4d5d5f6`, lending-v1 passed all twelve derivation controls, including exact
atomic extraction, and all twelve original V3 controls. The two unchanged saved
DSCR claims each scored 1.0 with verbatim extraction and correct arithmetic in
their reasons. This addresses the observed narrow calculation failure.
The same six full JSON packets remain unsuitable for population scoring: the
first received 1.0 while extraction omitted its displayed DSCR, and the second
stopped on incomplete generation. Four packets were not run. All runs retain
stable provenance. Explicit field coverage and bounded evaluation units are
needed before a whole-packet metric; none of these pilots establish one.

## Field coverage export

`export_field_units.py` reads saved benchmark records, canonical pages and authored
scope. It exports every cited claim and financial metric at a stable packet field
path. Each context contains only the exact cited spans; an uncited retrieved page
cannot supply missing support. Values are preserved, including incorrect claim
text with structurally valid citations, so export cannot inflate scores by dropping
wrong answers. Metric sentences retain displayed precision and carry exact
extraction expectations, which do not assert that the calculation is correct.

The private `coverage.json` accounts for all 19 current Packet fields and rejects
schema drift. Empty arrays, identity, source evidence and execution metadata are
identified. Disposition, missing-document statements, advice, questions and
abstention retain their values with pending-rubric status. The manifest reports
zero scored units after export. Export coverage is not semantic scoring coverage.

```powershell
python infra/evaluation/export_field_units.py --gold evals/gold_cases.jsonl --pages ../resources/credit_lens/corpus/pages.jsonl --records ../resources/credit_lens/evals/neural-linux-51a2c75/records.jsonl --case-ids creditlens-001,creditlens-101,creditlens-156,creditlens-176,creditlens-196,creditlens-231 --output ../resources/credit_lens/evals/new-field-export
python -m unittest discover -s infra/evaluation -p test_field_units.py -v
```

Use the main project environment for export. The separate semantic environment
runs the judge. Freeze pilot unit IDs before inference and retain every unselected
or failed unit explicitly. Claim-level scores use a different denominator from
the earlier whole-JSON pilots; they cannot be reported as whole-packet pass rates.

The export at `64dc55c` accounts for all 19 fields in each of the six frozen
packets and yields 85 cited claim/metric units. A preselected pilot includes the
first cited claim per packet and all three metrics: nine units total. All nine
completed with faithfulness 1.0 under lending-v1, and each metric was extracted
verbatim. The 18 raw responses match the retained outputs. This selected pilot
has no population interpretation: 76 units remain unselected, and 26 nonempty
field entries still need rubrics. The private score-coverage ledger preserves
those counts and keeps whole-packet scoring false.

`check_policy_fields.py` separately grades the frozen twenty-case policy-reference
contract in `evals/policy_lookup_expectations-v1.jsonl`. It checks disposition,
abstention, calculated metrics, missing documents and policy-evidence presence
against saved records, retaining case/field failures and input hashes. The baseline
passes 6/20; the routing change at `00cb2da` passes 20/20. This operational check
does not replace semantic judgment or alter the historical fixture rubric.

`reconcile_field_runs.py` combines terminal RAGAS runs against one frozen field
export. Supply `--export`, `--runs` and a fresh private `--output`. The runs file
is a JSON list of objects with `cases` (the frozen JSONL path) and `run` (the
directory containing `summary.json` and `judge-calls.jsonl`). Model, profile,
instruction and evaluator source identities must match across runs. The checker
rejects duplicate units, altered inputs and raw-output mismatches. Failed and
unrun units stay in the coverage ledger; failed expected extraction is distinct
from scored coverage. Five adversarial tests pass, and it reproduces the original
nine-unit pilot ledger. Within-field extraction completeness remains unproven.

The first two additional batches bring the historical sample to 57/85 scored
cited units, with 28 not run and 26 nonempty fields still awaiting rubrics.
Inspection found four extraction/NLI false positives: a document-version header
became an invented directory-location claim. The original answers did not contain
that claim. Scores and source-linked findings are preserved; these scores cannot
be promoted as validated project quality.

`run_ragas.py --unit-mode verbatim` provides a separate experimental metric using
RAGAS's NLI stage and score calculation. It passes the entire unchanged field as
one statement, bypassing generative extraction. One binary verdict must cover the
exact complete field. It uses one model call per unit and records a distinct
metric identity; its denominator differs from atomic-claim faithfulness. The
default remains `--unit-mode extracted`. Twelve frozen controls in
`controls-field-support-v1.jsonl` test compound support and unsupported additions.
Protocol and reconciliation checks pass; judge calibration is a separate step.

Next: inspect real packet judgments and calibrate against human judgments before
promoting semantic results. Public
benchmarks, full RAGAS validation, online replay/alerts and regression gates remain required by
the broader evaluation plan. Read [observability](../../docs/observability.md)
and [failure modes](../../docs/failure-modes.md) for that next layer.

Contracts: [DeepEval custom models](https://deepeval.com/guides/guides-using-custom-llms),
[Faithfulness](https://deepeval.com/docs/metrics-faithfulness),
[Ollama chat API](https://docs.ollama.com/api/chat).
RAGAS contracts: [Faithfulness pipeline](https://github.com/vibrantlabsai/ragas/tree/v0.4.3/src/ragas/metrics/collections/faithfulness)
and [structured model interface](https://github.com/vibrantlabsai/ragas/blob/v0.4.3/src/ragas/llms/base.py).
