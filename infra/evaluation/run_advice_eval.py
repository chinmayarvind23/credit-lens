"""Run source-grounded DeepEval GEval for actions and questions using frozen guidance."""

import argparse
import json
import sys
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

from advice_rubric import RUBRICS, VERSION, validate_advice_score
from run_deepeval import restrict_network
from run_ragas import read_cases


def verify_journal(path: Path, results: list[dict]) -> int:
    """Require one actual binary response per field and reject raw boolean/number coercion."""
    calls = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if len(calls) != len(results):
        raise ValueError("Advice evaluation must use exactly one raw call per field")
    for call, result in zip(calls, results, strict=True):
        response = call["response"]
        raw = json.loads(response["message"]["content"])
        if response.get("done") is not True or response.get("done_reason") != "stop":
            raise ValueError("Advice generation was incomplete")
        if set(raw) != {"score", "reason"} or type(raw["score"]) not in (int, float):
            raise ValueError("Raw advice response has invalid keys or score type")
        if raw != result["outputs"][0]["output"]:
            raise ValueError("Raw advice response differs from its retained output")
    return len(calls)


def run(args: argparse.Namespace) -> None:
    """Freeze evaluator identity and keep failed cases visible without changing their answers."""
    repo = Path(__file__).resolve().parents[2]
    if args.output.resolve().is_relative_to(repo):
        raise ValueError("Advice evidence must stay outside the repository")
    inputs, cases = read_cases(args.cases)
    steps = RUBRICS[args.rubric]
    for case in cases:
        if not isinstance(case.get("expected_output"), str) or not case["expected_output"].strip():
            raise ValueError("Advice cases require explicit expected guidance")
    sys.addaudithook(restrict_network)
    # The local transport disables DeepEval telemetry/dotenv before the SDK import.
    from advice_judge import AdviceJudge  # noqa: I001

    from deepeval.metrics import GEval
    from deepeval.test_case import LLMTestCase, SingleTurnParams

    args.output.mkdir(parents=True, exist_ok=False)
    sdk_dir = Path(sys.modules[GEval.__module__].__file__).parent
    sources = {
        name: Path(__file__).with_name(name)
        for name in (
            "run_advice_eval.py",
            "advice_rubric.py",
            "advice_judge.py",
            "local_judge.py",
            "run_deepeval.py",
            "run_ragas.py",
            "ragas_profiles.py",
            "uv.lock",
        )
    }
    sources.update(
        {
            "geval": sdk_dir / "g_eval.py",
            "schema": sdk_dir / "schema.py",
            "utils": sdk_dir / "utils.py",
            "template": sdk_dir / "templates/generate_strict_evaluation_results.txt",
        }
    )
    before = {key: sha256(path.read_bytes()).hexdigest() for key, path in sources.items()}
    manifest = {
        "status": "running",
        "deepeval": version("deepeval"),
        "metric": "GEval",
        "rubric_version": args.rubric,
        "evaluation_steps": list(steps),
        "strict_mode": True,
        "source_hashes": before,
        "input_sha256": sha256(inputs).hexdigest(),
        "model": args.model,
        "digest": args.digest,
        "case_count": len(cases),
        "results": [],
        "human_calibrated": False,
        "network": "127.0.0.1:11434 only",
    }
    summary_path = args.output / "summary.json"
    summary_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    judge = AdviceJudge(args.model, args.digest, args.output / "judge-calls.jsonl")
    try:
        for case in cases:
            judge.outputs.clear()
            result = {"id": case["id"], "expected_range": case.get("expected_range")}
            manifest["results"].append(result)
            try:
                metric = GEval(
                    name=args.rubric,
                    model=judge,
                    evaluation_steps=list(steps),
                    evaluation_params=[
                        SingleTurnParams.INPUT,
                        SingleTurnParams.ACTUAL_OUTPUT,
                        SingleTurnParams.EXPECTED_OUTPUT,
                        SingleTurnParams.CONTEXT,
                    ],
                    strict_mode=True,
                    async_mode=False,
                )
                score = metric.measure(
                    LLMTestCase(
                        input=case["input"],
                        actual_output=case["actual_output"],
                        expected_output=case["expected_output"],
                        context=case["retrieval_context"],
                    ),
                    _show_indicator=False,
                )
                result.update(score=score, reason=metric.reason)
                validate_advice_score(score, metric.reason, judge.outputs)
                result["structure_valid"] = True
            finally:
                result["outputs"] = list(judge.outputs)
                summary_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        manifest["raw_calls_verified"] = verify_journal(
            args.output / "judge-calls.jsonl", manifest["results"]
        )
        controls = [r for r in manifest["results"] if r["expected_range"] is not None]
        manifest["controls_passed"] = (
            all(r["expected_range"][0] <= r["score"] <= r["expected_range"][1] for r in controls)
            if controls
            else None
        )
        if manifest["controls_passed"] is False:
            raise ValueError("Advice controls failed; do not promote the judge")
        manifest["status"] = "completed"
    except Exception as error:
        manifest.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        judge.close()
        manifest["provenance_stable"] = args.cases.read_bytes() == inputs and before == {
            key: sha256(path.read_bytes()).hexdigest() for key, path in sources.items()
        }
        changed = not manifest["provenance_stable"] and manifest["status"] == "completed"
        if changed:
            manifest.update(status="failed", error="Advice evaluation provenance changed")
        summary_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if changed:
            raise ValueError("Advice evaluation provenance changed")


def main() -> None:
    """Require frozen cases, an explicit local model identity and a fresh private output path."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cases", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--rubric", choices=tuple(RUBRICS), default=VERSION)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
