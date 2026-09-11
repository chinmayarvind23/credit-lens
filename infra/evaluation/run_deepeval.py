"""Run source-grounded DeepEval judgments with no remote sockets or paid model fallback."""

import argparse
import json
import sys
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from typing import Any


def restrict_network(event: str, args: tuple[Any, ...]) -> None:
    """Enforce the process network boundary before importing evaluation libraries."""
    if event == "socket.connect" and args[1] != ("127.0.0.1", 11434):
        raise PermissionError("Evaluation permits only the local Ollama endpoint")


def run(args: argparse.Namespace) -> None:
    """Keep controls, errors and raw judgments separate from unmeasured project success targets."""
    repo = Path(__file__).resolve().parents[2]
    if args.output.resolve().is_relative_to(repo):
        raise ValueError("Evaluation journals must stay outside the source repository")
    sys.addaudithook(restrict_network)
    from deepeval.metrics import FaithfulnessMetric
    from deepeval.test_case import LLMTestCase
    from local_judge import LocalJudge

    args.output.mkdir(parents=True, exist_ok=False)
    inputs = args.cases.read_bytes()
    if len(inputs) > 8_000_000:
        raise ValueError("Evaluation input exceeds eight megabytes")
    cases = [json.loads(line) for line in inputs.decode().splitlines() if line.strip()]
    if not 1 <= len(cases) <= 24:
        raise ValueError("The initial judge screen accepts between one and 24 cases")
    sources = [
        Path(__file__),
        Path(__file__).with_name("local_judge.py"),
        Path(__file__).with_name("uv.lock"),
    ]
    before = {p.name: sha256(p.read_bytes()).hexdigest() for p in sources}
    manifest = {
        "status": "running",
        "deepeval": version("deepeval"),
        "input_sha256": sha256(inputs).hexdigest(),
        "source_hashes": before,
        "model": args.model,
        "digest": args.digest,
        "metric": "FaithfulnessMetric",
        "threshold": 0.9,
        "penalize_ambiguous_claims": True,
        "human_calibrated": False,
        "network": "127.0.0.1:11434 only",
        "results": [],
    }
    judge = LocalJudge(args.model, args.digest, args.output / "judge-calls.jsonl", calls=100)
    try:
        for case in cases:
            metric = FaithfulnessMetric(
                model=judge,
                threshold=0.9,
                async_mode=False,
                include_reason=False,
                penalize_ambiguous_claims=True,
            )
            score = metric.measure(
                LLMTestCase(
                    input=case["input"],
                    actual_output=case["actual_output"],
                    retrieval_context=case["retrieval_context"],
                ),
                _show_indicator=False,
            )
            manifest["results"].append(
                {
                    "id": case["id"],
                    "score": score,
                    "claims": metric.claims,
                    "truths": metric.truths,
                    "verdicts": [v.model_dump() for v in metric.verdicts],
                    "expected_range": case.get("expected_range"),
                }
            )
            (args.output / "summary.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )
        controls = [r for r in manifest["results"] if r["expected_range"] is not None]
        manifest["controls_passed"] = (
            all(
                r["expected_range"][0] <= r["score"] <= r["expected_range"][1] and bool(r["claims"])
                for r in controls
            )
            if controls
            else None
        )
        if manifest["controls_passed"] is False:
            raise ValueError("Judge screening controls failed; do not promote these judgments")
        manifest["status"] = "completed"
    except Exception as error:
        manifest.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        judge.close()
        manifest["provenance_stable"] = (
            before == {p.name: sha256(p.read_bytes()).hexdigest() for p in sources}
            and args.cases.read_bytes() == inputs
        )
        (args.output / "summary.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def main() -> None:
    """Require explicit model identity, input cases and a fresh private output directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--digest", required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
