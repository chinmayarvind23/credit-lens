"""Run RAGAS faithfulness with a pinned local judge and inspectable statement coverage."""

import argparse
import asyncio
import json
import math
import sys
from collections import Counter
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from typing import Any

from ragas_profiles import configure
from run_deepeval import restrict_network


def validate_score(score: float, outputs: list[dict]) -> None:
    """Reject empty, omitted or nonbinary judgments instead of silently changing denominators."""
    if [item["schema"] for item in outputs] != ["StatementGeneratorOutput", "NLIStatementOutput"]:
        raise ValueError("Expected both RAGAS extraction and verdict outputs")
    statements = outputs[0]["output"]["statements"]
    verdicts = outputs[1]["output"]["statements"]
    if not statements or any(not statement.strip() for statement in statements):
        raise ValueError("Empty claim extraction cannot establish faithfulness")
    if Counter(statements) != Counter(v["statement"] for v in verdicts):
        raise ValueError("Verdicts must cover every extracted statement exactly once")
    if any(type(v["verdict"]) is not int or v["verdict"] not in (0, 1) for v in verdicts):
        raise ValueError("RAGAS verdicts must be binary")
    expected = sum(v["verdict"] for v in verdicts) / len(statements)
    if not math.isfinite(score) or not math.isclose(score, expected, abs_tol=1e-12):
        raise ValueError("RAGAS score does not match the retained verdicts")


def read_cases(path: Path) -> tuple[bytes, list[dict[str, Any]]]:
    """Bound and validate frozen inputs before creating outputs or requesting inference."""
    inputs = path.read_bytes()
    if len(inputs) > 8_000_000:
        raise ValueError("Evaluation input exceeds eight megabytes")
    cases = [json.loads(line) for line in inputs.decode().splitlines() if line.strip()]
    if not 1 <= len(cases) <= 24 or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("Require one to 24 uniquely identified cases")
    for case in cases:
        if any(
            not isinstance(case[k], str) or not case[k].strip()
            for k in ("id", "input", "actual_output")
        ):
            raise ValueError("Case text fields must be nonempty strings")
        contexts = case["retrieval_context"]
        if (
            not isinstance(contexts, list)
            or not contexts
            or any(not isinstance(c, str) or not c.strip() for c in contexts)
        ):
            raise ValueError("Each case requires nonempty source contexts")
        bounds = case.get("expected_range")
        if bounds is not None and (len(bounds) != 2 or not 0 <= bounds[0] <= bounds[1] <= 1):
            raise ValueError("Invalid screening range")
        statements = case.get("expected_statements")
        if statements is not None and (
            not isinstance(statements, list)
            or not statements
            or any(not isinstance(s, str) or not s.strip() for s in statements)
        ):
            raise ValueError("Expected extraction must contain nonempty statements")
    return inputs, cases


def run(args: argparse.Namespace, loop: asyncio.AbstractEventLoop) -> None:
    """Journal library outputs and failures separately from unvalidated project quality claims."""
    repo = Path(__file__).resolve().parents[2]
    if args.output.resolve().is_relative_to(repo):
        raise ValueError("Evaluation journals must stay outside the source repository")
    inputs, cases = read_cases(args.cases)
    sys.addaudithook(restrict_network)
    # Privacy initialization must precede any RAGAS import, including transitive imports.
    from ragas_judge import LocalJudge, RagasJudge  # noqa: I001

    from ragas.metrics.collections import Faithfulness
    from ragas.metrics.collections.faithfulness import util

    args.output.mkdir(parents=True, exist_ok=False)
    sources = [
        Path(__file__).with_name(n)
        for n in (
            "run_ragas.py",
            "ragas_judge.py",
            "ragas_profiles.py",
            "local_judge.py",
            "run_deepeval.py",
            "uv.lock",
        )
    ]
    sources.extend([Path(sys.modules[Faithfulness.__module__].__file__), Path(util.__file__)])
    before = {p.name: sha256(p.read_bytes()).hexdigest() for p in sources}
    manifest: dict[str, Any] = {
        "status": "running",
        "ragas": version("ragas"),
        "source_hashes": before,
        "input_sha256": sha256(inputs).hexdigest(),
        "model": args.model,
        "digest": args.digest,
        "metric": "ragas.metrics.collections.Faithfulness",
        "profile": args.profile,
        "human_calibrated": False,
        "network": "127.0.0.1:11434 only; stdlib loop initialized before restriction",
        "case_count": len(cases),
        "results": [],
    }
    transport = LocalJudge(args.model, args.digest, args.output / "judge-calls.jsonl", calls=48)
    judge = RagasJudge(transport)
    (args.output / "summary.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    try:
        metric = Faithfulness(llm=judge)
        manifest["instruction_sha256"] = configure(metric, args.profile)
        for case in cases:
            judge.outputs.clear()
            result: dict[str, Any] = {
                "id": case["id"],
                "expected_range": case.get("expected_range"),
            }
            manifest["results"].append(result)
            try:
                score = float(
                    loop.run_until_complete(
                        metric.ascore(
                            user_input=case["input"],
                            response=case["actual_output"],
                            retrieved_contexts=case["retrieval_context"],
                        )
                    ).value
                )
                result["score"] = score if math.isfinite(score) else None
                validate_score(score, judge.outputs)
                result["structure_valid"] = True
                expected_statements = case.get("expected_statements")
                result["expected_statements"] = expected_statements
                result["extraction_matches"] = (
                    Counter(expected_statements)
                    == Counter(judge.outputs[0]["output"]["statements"])
                    if expected_statements is not None
                    else None
                )
            finally:
                result["outputs"] = list(judge.outputs)
                (args.output / "summary.json").write_text(
                    json.dumps(manifest, indent=2), encoding="utf-8"
                )
        controls = [r for r in manifest["results"] if r["expected_range"] is not None]
        manifest["controls_passed"] = (
            all(r["expected_range"][0] <= r["score"] <= r["expected_range"][1] for r in controls)
            if controls
            else None
        )
        extractions = [r for r in manifest["results"] if r.get("expected_statements") is not None]
        manifest["extraction_controls_passed"] = (
            all(r["extraction_matches"] for r in extractions) if extractions else None
        )
        if manifest["controls_passed"] is False or manifest["extraction_controls_passed"] is False:
            raise ValueError("Judge screening controls failed; do not promote these judgments")
        manifest["status"] = "completed"
    except Exception as error:
        manifest.update(status="failed", error_type=type(error).__name__, error=str(error))
        raise
    finally:
        transport.close()
        manifest["provenance_stable"] = (
            before == {p.name: sha256(p.read_bytes()).hexdigest() for p in sources}
            and args.cases.read_bytes() == inputs
        )
        changed = not manifest["provenance_stable"] and manifest["status"] == "completed"
        if changed:
            manifest.update(status="failed", error_type="ValueError", error="Provenance changed")
        (args.output / "summary.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        if changed:
            raise ValueError("Evaluation source or inputs changed during the run")


def main() -> None:
    """Create only stdlib loop sockets before the restriction, then execute serial bounded work."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--profile", choices=("stock", "lending-v1"), default="stock")
    args = parser.parse_args()
    loop = asyncio.new_event_loop()
    try:
        run(args, loop)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
