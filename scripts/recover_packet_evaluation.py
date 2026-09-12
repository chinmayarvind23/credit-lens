"""Recover only unstarted GEval batches with explicit empty-evidence input validation."""

import argparse
import json
import sys
from hashlib import sha256
from pathlib import Path


def digest(path: Path) -> str:
    """Bind the recovery adapter, original run and every retained artifact to exact bytes."""
    return sha256(path.read_bytes()).hexdigest()


def read_whole_cases(path: Path) -> tuple[bytes, list[dict]]:
    """Permit an empty context list for complete-packet refusals without inventing evidence."""
    inputs = path.read_bytes()
    if len(inputs) > 8_000_000:
        raise ValueError("Evaluation input exceeds eight megabytes")
    cases = [json.loads(line) for line in inputs.decode().splitlines() if line.strip()]
    if not 1 <= len(cases) <= 24 or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("Require one to 24 uniquely identified cases")
    for case in cases:
        if any(
            not isinstance(case[k], str) or not case[k].strip()
            for k in ("id", "input", "actual_output", "expected_output")
        ):
            raise ValueError("Case text fields must be nonempty strings")
        contexts = case["retrieval_context"]
        if not isinstance(contexts, list) or any(
            not isinstance(context, str) or not context.strip() for context in contexts
        ):
            raise ValueError("Contexts must be a list of nonempty source strings")
        bounds = case.get("expected_range")
        if bounds is not None and (len(bounds) != 2 or not 0 <= bounds[0] <= bounds[1] <= 1):
            raise ValueError("Invalid screening range")
        statements = case.get("expected_statements")
        if statements is not None and (
            not isinstance(statements, list)
            or not statements
            or any(
                not isinstance(statement, str) or not statement.strip() for statement in statements
            )
        ):
            raise ValueError("Expected extraction must contain nonempty statements")
    return inputs, cases


def verify_retained(summary: dict) -> None:
    """Recovery may only add never-started batches to byte-identical completed evidence."""
    for batch in summary["batches"]:
        directory = Path(batch["run"])
        if batch["status"] != "completed" and (directory.exists() or batch["artifact_hashes"]):
            raise ValueError("Do not selectively regrade partially evaluated batches")
        if any(
            digest(directory / name) != expected
            for name, expected in batch["artifact_hashes"].items()
        ):
            raise ValueError("Retained completed artifact changed")


def recover(original: Path, output: Path) -> None:
    """Reuse complete batches only; never overwrite a failed run or selectively replace verdicts."""
    repo = Path(__file__).resolve().parents[1]
    if output.exists() or output.resolve().is_relative_to(repo):
        raise ValueError("Choose a fresh private recovery directory")
    source = original / "summary.json"
    summary = json.loads(source.read_text(encoding="utf-8"))
    if summary["status"] != "failed" or not summary.get("input_stable"):
        raise ValueError("Recovery requires a terminal source-stable failed run")
    if summary["metric"] != "whole-packet-lending-v2":
        raise ValueError("Only the explicitly reviewed v2 packet recovery is supported")
    if any(digest(Path(path)) != expected for path, expected in summary["input_hashes"].items()):
        raise ValueError("Frozen evaluator or input changed")
    verify_retained(summary)
    output.mkdir(parents=True)
    summary["status"] = "running"
    summary["recovery"] = {
        "original_summary": str(source.resolve()),
        "original_sha256": digest(source),
        "adapter": str(Path(__file__).resolve()),
        "adapter_sha256": digest(Path(__file__)),
        "change": (
            "Accept empty evidence lists for whole-packet refusals; "
            "preserve exact cases, rubric and prompts"
        ),
        "reuses_completed_verdicts": True,
    }
    summary["input_hashes"].update(
        {
            str(source.resolve()): digest(source),
            str(Path(__file__).resolve()): digest(Path(__file__)),
        }
    )
    sys.path.insert(0, str(repo / "infra/evaluation"))
    import run_advice_eval  # noqa: PLC0415

    # This adapter is part of the recovery manifest, not an unrecorded evaluator replacement.
    run_advice_eval.read_cases = read_whole_cases
    identity = json.loads((Path(summary["batches"][0]["run"]) / "summary.json").read_text())
    target = output / "summary.json"
    try:
        for batch in summary["batches"]:
            if batch["status"] == "completed":
                continue
            batch["run"] = str((output / Path(batch["run"]).name).resolve())
            target.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            run_advice_eval.run(
                argparse.Namespace(
                    cases=Path(batch["cases"]),
                    output=Path(batch["run"]),
                    model=identity["model"],
                    digest=identity["digest"],
                    rubric=summary["metric"],
                )
            )
            batch.update(
                status="completed",
                artifact_hashes={
                    path.name: digest(path)
                    for path in Path(batch["run"]).iterdir()
                    if path.is_file()
                },
            )
            batch.pop("error_type", None)
            print(json.dumps({"recovered": Path(batch["run"]).name}), flush=True)
        summary["status"] = "completed"
    finally:
        summary["input_stable"] = all(
            digest(Path(path)) == expected for path, expected in summary["input_hashes"].items()
        )
        if summary["status"] != "completed" or not summary["input_stable"]:
            summary["status"] = "failed"
        target.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def main() -> None:
    """Require explicit original run and new private output paths."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    recover(args.original, args.output)


if __name__ == "__main__":
    main()
