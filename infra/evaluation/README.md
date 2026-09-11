# Local semantic evaluation

This isolated environment uses DeepEval 4.2.2 without changing serving dependencies.
It has run actual FaithfulnessMetric judgments through an installed local model.
The first judge screen **failed**: Llama 3.1 8B Q4_K_M scored both a supported and
contradicted control 0.5, incorrectly calling the matching 1.25 policy minimum a
contradiction. Llama 3.2 3B also failed its supported control. Qwen3 8B subsequently
passed all twelve frozen V3 controls, scoring supported claims 1 and contradicted
claims 0. The V3 dates are explicit in the judged claims because Faithfulness does
not use the question to resolve dates. Earlier controls and failed runs are retained.
This clears authored screening only. Human calibration, RAGAS and the full 240-case
semantic baseline remain unfinished.

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

Next: inspect real packet judgments and calibrate against human judgments before
promoting semantic results. Public
benchmarks, RAGAS, online replay/alerts and regression gates remain required by
the broader evaluation plan. Read [observability](../../docs/observability.md)
and [failure modes](../../docs/failure-modes.md) for that next layer.

Contracts: [DeepEval custom models](https://deepeval.com/guides/guides-using-custom-llms),
[Faithfulness](https://deepeval.com/docs/metrics-faithfulness),
[Ollama chat API](https://docs.ollama.com/api/chat).
