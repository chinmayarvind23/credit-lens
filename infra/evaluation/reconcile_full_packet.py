"""Reconcile full-packet GEval outputs against complete inputs, actual prompts and raw responses."""

import argparse
import json
import sys
from hashlib import sha256
from pathlib import Path


def digest(path):
    """Check raw file identities before interpreting scores."""
    return sha256(path.read_bytes()).hexdigest()


def read_batch(batch, index, population, first_identity, sdk, rubric="whole-packet-lending-v1"):
    """Bind complete raw calls and input IDs to one unchanged SDK and model identity."""
    cases_path = Path(batch["cases"])
    directory = Path(batch["run"])
    if any(digest(directory / name) != h for name, h in batch["artifact_hashes"].items()):
        raise ValueError("Completed batch artifacts changed")
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    cases = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines()]
    calls = [
        json.loads(line)
        for line in (directory / "judge-calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    results = summary["results"]
    if not summary.get("provenance_stable") or summary["input_sha256"] != digest(cases_path):
        raise ValueError("Batch input or evaluator provenance changed")
    if not len(cases) == len(calls) == len(results):
        raise ValueError("Missing raw judgments or ungraded cases")
    if index and (
        [c["id"] for c in cases] != population["batches"][index - 1]["ids"]
        or digest(cases_path) != population["batches"][index - 1]["sha256"]
    ):
        raise ValueError("Batch differs from the complete population")
    identity = {
        k: summary[k]
        for k in (
            "model",
            "digest",
            "source_hashes",
            "deepeval",
            "evaluation_steps",
            "rubric_version",
        )
    }
    if (first_identity is not None and identity != first_identity) or identity[
        "rubric_version"
    ] != rubric:
        raise ValueError("Evaluator identity differs across batches")
    sdk_files = {
        "geval": sdk / "g_eval.py",
        "schema": sdk / "schema.py",
        "utils": sdk / "utils.py",
        "template": sdk / "templates/generate_strict_evaluation_results.txt",
    }
    if any(digest(p) != identity["source_hashes"][k] for k, p in sdk_files.items()):
        raise ValueError("Installed GEval changed")
    return summary, cases, calls, results, identity


def validate_call(case, call, result, prompt, model, validate_score):
    """Reject prompt swaps, changed reasons and coerced or incomplete raw verdicts."""
    response = call["response"]
    raw = json.loads(response["message"]["content"])
    if call["prompt"] != prompt or result["id"] != case["id"]:
        raise ValueError("Raw prompt or case identity mismatch")
    if (
        response.get("done") is not True
        or response.get("done_reason") != "stop"
        or (response.get("model") != model)
    ):
        raise ValueError("Wrong model or incomplete generation")
    if set(raw) != {"score", "reason"} or raw != result["outputs"][0]["output"]:
        raise ValueError("Raw output differs from GEval output")
    validate_score(result["score"], result["reason"], result["outputs"])
    if type(raw["score"]) not in (int, float):
        raise ValueError("Raw score must be a number, not a boolean")
    row = {
        "id": case["id"],
        "score": raw["score"],
        "reason": raw["reason"],
        "prompt_sha256": sha256(prompt.encode()).hexdigest(),
    }
    return row


def offline(event, args):
    """The verifier reads evidence and reconstructs prompts without model or network calls."""
    if event == "socket.connect":
        raise PermissionError("Reconciliation makes no network calls")


def verify_registered_batch(batch, run):
    """Every graded input must be one of the files frozen before model execution."""
    path = Path(batch["cases"]).resolve()
    if digest(path) != run["input_hashes"].get(str(path)):
        raise ValueError("Unregistered batch input")


def reconcile(population_dir, run_dir, output):
    """Require terminal complete grading; failed controls preserve only a diagnostic raw rate."""
    if output.exists():
        raise ValueError("Use a fresh reconciliation file")
    run = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    rubric = run["metric"]
    if rubric not in {"whole-packet-lending-v1", "whole-packet-lending-v2"}:
        raise ValueError("Unknown whole-packet rubric")
    population_path = population_dir / "manifest.json"
    population = json.loads(population_path.read_text(encoding="utf-8"))
    if run["status"] == "running" or not run.get("input_stable"):
        raise ValueError("Wait for a terminal, source-stable run")
    if digest(population_path) != run["population_sha256"] or any(
        digest(Path(p)) != h for p, h in run["input_hashes"].items()
    ):
        raise ValueError("Frozen population or evaluator bytes changed")
    if len(run["batches"]) != len(population["batches"]) + 1:
        raise ValueError("Whole-packet grading is incomplete")
    # Disable SDK telemetry before import and prohibit inference during reconciliation.
    from local_judge import LocalJudge  # noqa: I001
    from advice_rubric import RUBRICS, validate_advice_score
    from deepeval.metrics import GEval
    from deepeval.metrics.g_eval.utils import (
        construct_g_eval_params_string,
        construct_test_case_string,
        number_evaluation_steps,
    )
    from deepeval.test_case import LLMTestCase, SingleTurnParams

    sys.addaudithook(offline)
    params = [
        SingleTurnParams.INPUT,
        SingleTurnParams.ACTUAL_OUTPUT,
        SingleTurnParams.EXPECTED_OUTPUT,
        SingleTurnParams.CONTEXT,
    ]
    all_results, identities = [], []
    controls = []
    for index, batch in enumerate(run["batches"]):
        verify_registered_batch(batch, run)
        summary, cases, calls, results, identity = read_batch(
            batch,
            index,
            population,
            identities[0] if identities else None,
            Path(sys.modules[GEval.__module__].__file__).parent,
            rubric,
        )
        identities.append(identity)
        judge = LocalJudge(
            summary["model"], summary["digest"], output.with_suffix(".unused"), calls=1
        )
        try:
            metric = GEval(
                name=rubric,
                model=judge,
                evaluation_steps=list(RUBRICS[rubric]),
                evaluation_params=params,
                strict_mode=True,
                async_mode=False,
            )
            for case, call, result in zip(cases, calls, results, strict=True):
                test = LLMTestCase(
                    input=case["input"],
                    actual_output=case["actual_output"],
                    expected_output=case["expected_output"],
                    context=case["retrieval_context"],
                )
                prompt = metric._get_prompt(
                    "generate_strict_evaluation_results",
                    evaluation_steps=number_evaluation_steps(metric.evaluation_steps),
                    test_case_content=construct_test_case_string(params, test),
                    parameters=construct_g_eval_params_string(params),
                    _additional_context=None,
                    multimodal=False,
                )
                row = validate_call(
                    case, call, result, prompt, summary["model"], validate_advice_score
                )
                if index == 0:
                    row["expected_range"] = case["expected_range"]
                    controls.append(row)
                else:
                    all_results.append(row)
        finally:
            judge.close()
    expected = [r["id"] for r in population["ledger"] if r["status"] == "exported"]
    if [r["id"] for r in all_results] != expected or len(set(expected)) != len(expected):
        raise ValueError("Population coverage differs")
    controls_pass = all(
        r["expected_range"][0] <= r["score"] <= r["expected_range"][1] for r in controls
    )
    report = {
        "status": "reconciled",
        "case_count": population["case_count"],
        "graded_packets": len(all_results),
        "ledger": population["ledger"],
        "raw_passes": sum(r["score"] for r in all_results),
        "raw_packet_pass_rate": sum(r["score"] for r in all_results) / len(all_results),
        "controls_passed": controls_pass,
        "controls": controls,
        "results": all_results,
        "human_calibrated": False,
        "validated_project_quality": False,
        "identity": identities[0],
        "run_summary_sha256": digest(run_dir / "summary.json"),
    }
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k not in {"ledger", "results", "identity", "controls"}
            },
            indent=2,
        )
    )


def main():
    """Require private frozen inputs and a fresh output report."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("population", "run", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    reconcile(args.population, args.run, args.output)


if __name__ == "__main__":
    main()
