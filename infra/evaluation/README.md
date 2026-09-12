# Local evaluation tools

This separate environment runs DeepEval and RAGAS through an existing local Ollama
endpoint at `127.0.0.1:11434`. It does not start the service, download models or use
a hosted fallback. Keep serving dependencies in the main project environment.

## Install and select a model

```powershell
$env:UV_PROJECT_ENVIRONMENT='C:/creditlens-work/semantic-venv'
uv sync --project infra/evaluation --locked
```

Read the local `/api/tags` response and supply the installed model's exact digest.
Cloud tags, remote descriptors and changed digests are rejected. The runner disables
telemetry and dotenv loading and restricts sockets to the configured loopback server.
Use a trusted local server and local weights; this process restriction is not an OS sandbox.

## Controls and individual units

Use a fresh output directory outside the repository:

```powershell
C:/creditlens-work/semantic-venv/Scripts/python.exe infra/evaluation/run_deepeval.py --cases infra/evaluation/controls-v3.jsonl --model <installed-model> --digest <installed-digest> --output C:/creditlens-work/deepeval-screen
C:/creditlens-work/semantic-venv/Scripts/python.exe infra/evaluation/run_ragas.py --cases infra/evaluation/controls-v3.jsonl --model <installed-model> --digest <installed-digest> --output C:/creditlens-work/ragas-screen
```

Rows contain `id`, `input`, `actual_output`, `retrieval_context`, and optional
`expected_range`. Context must come from source evidence. Incomplete generation,
malformed output and failed controls return nonzero. Preserve failed and unrun units.

RAGAS supports `--profile stock` and `--profile lending-v1`. The latter adds explicit
derivation and extraction instructions. `--unit-mode extracted` uses the library's
extraction stage; `--unit-mode verbatim` sends the unchanged complete field to NLI.
These modes have different units and must not be mixed in one reconciliation.

Runs are sequential and record prompts, responses, model identity, source/input
hashes and completion state. HTTP and generation bounds are enforced by the runner;
an HTTP timeout does not provide a whole-process wall-clock deadline.

## Export and reconcile

Use the main environment for exports and the separate semantic environment for inference.
Exports verify canonical spans, page metadata, scope and saved citations. They retain
original output text and account for missing, denied and invalid packets.

| Tool | Purpose |
| --- | --- |
| `export_packets.py` | Export selected saved packets with canonical source context |
| `export_field_units.py` | Export cited claims and financial fields at stable field paths |
| `export_population.py` | Export all saved cases into bounded input shards |
| `run_population.py` | Run local field judgments using a compatible control summary |
| `reconcile_field_runs.py` | Check compatible model/rubric/input identities and raw output coverage |
| `check_policy_fields.py` | Check saved outputs against policy-reference expectations |
| `check_packet_state.py` | Check disposition, missing-document and abstention fields |
| `export_advice_fields.py` / `run_advice_eval.py` | Export and judge guidance under its own rubric |
| `run_full_packet.py` / `reconcile_full_packet.py` | Export, judge and reconcile complete packets |

```powershell
python infra/evaluation/export_population.py --gold evals/gold_cases.jsonl --pages <pages.jsonl> --records <saved-records.jsonl> --output <fresh-input-directory>
<semantic-python> infra/evaluation/run_population.py --population <input-directory> --controls <compatible-control-summary.json> --model <installed-model> --digest <installed-digest> --output <fresh-run-directory>
python infra/evaluation/run_full_packet.py export --gold evals/gold_cases.jsonl --pages <pages.jsonl> --records <saved-records.jsonl> --output <fresh-packet-inputs>
<semantic-python> infra/evaluation/run_full_packet.py run --population <packet-inputs> --controls infra/evaluation/controls-whole-packet-v1.jsonl --model <installed-model> --digest <installed-digest> --output <fresh-packet-run>
<semantic-python> infra/evaluation/reconcile_full_packet.py --population <packet-inputs> --run <terminal-run> --output <fresh-report.json>
```

Consult each command's `--help` for required inputs. Reconciliation rejects altered
inputs, incompatible evaluator identities, duplicate units and raw-output mismatches.
Field support, packet judgments, deterministic state checks and human calibration are
separate concepts. A structurally valid model judgment is not proof of semantic correctness.

## Explicit interrupted-run recovery

Inspect the terminal run before restarting. The recovery adapter can preserve completed
whole-packet batches and handle unstarted batches with empty evidence lists:

```powershell
<semantic-python> scripts/recover_packet_evaluation.py --original <terminal-run> --output <fresh-derived-run>
<semantic-python> infra/evaluation/reconcile_full_packet.py --population <frozen-inputs> --run <derived-run> --output <fresh-report.json>
```

The adapter rejects partially evaluated batches, changed inputs and reused output paths.
Keep its derived manifest with all referenced original child summaries. It is an explicit
operator action, not an automatic retry policy or permission to selectively regrade cases.

## Tests and references

```powershell
<semantic-python> -m unittest discover -s infra/evaluation -p "test_*.py" -v
```

See the [documentation index](../../docs/README.md) and [evaluation inputs](../../evals/README.md).
Library contracts: [DeepEval custom models](https://deepeval.com/guides/guides-using-custom-llms),
[Faithfulness](https://deepeval.com/docs/metrics-faithfulness), [Ollama chat](https://docs.ollama.com/api/chat),
and [RAGAS Faithfulness](https://github.com/vibrantlabsai/ragas/tree/v0.4.3/src/ragas/metrics/collections/faithfulness).
