"""Account for every exported unit across bounded RAGAS runs, including failed runs."""

import argparse
import json
from collections import Counter
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any

from run_ragas import validate_field_score, validate_score

IDENTITY = ("model", "digest", "profile", "metric", "source_hashes", "instruction_sha256")


def reconcile_run(cases: list[dict], summary: dict, raw: list[dict]) -> dict[str, dict]:
    """Require terminal, stable evidence and exact raw outputs before counting any score."""
    if summary["status"] not in {"completed", "failed"} or not summary["provenance_stable"]:
        raise ValueError("Only terminal runs with stable provenance can be reconciled")
    ids = [case["id"] for case in cases]
    results = summary["results"]
    result_ids = [result["id"] for result in results]
    if not 1 <= len(ids) <= 24 or len(set(ids)) != len(ids) or result_ids != ids[: len(results)]:
        raise ValueError("Results must be a unique ordered prefix of frozen cases")
    if summary["case_count"] != len(cases):
        raise ValueError("Run case count differs from its inputs")
    complete = summary["status"] == "completed"
    if complete and len(results) != len(cases):
        raise ValueError("A completed run must include every scheduled case")
    outputs = [output for result in results for output in result["outputs"]]
    if len(raw) != len(outputs) and not (not complete and len(raw) == len(outputs) + 1):
        raise ValueError("Raw call count differs from parsed outputs and terminal failure")
    for call, output in zip(raw, outputs, strict=False):
        if (
            call["response"].get("done") is not True
            or call["response"].get("done_reason") != "stop"
        ):
            raise ValueError("Parsed output came from an incomplete raw generation")
        actual = json.loads(call["response"]["message"]["content"])
        if json.dumps(actual, sort_keys=True) != json.dumps(output["output"], sort_keys=True):
            raise ValueError("Retained outputs differ from the raw model journal")
    ledger = {identity: {"status": "unrun", "score": None} for identity in ids}
    for case, result in zip(cases, results, strict=False):
        ledger[result["id"]] = result_row(
            case, result, complete, summary.get("unit_mode", "extracted")
        )
    return ledger


def result_row(case: dict, result: dict, complete: bool, mode: str) -> dict[str, Any]:
    """Keep failed extraction separate even when the judge emits a plausible score."""
    if not result.get("structure_valid"):
        if complete:
            raise ValueError("Completed runs require structurally valid results")
        return {"status": "failed", "score": None}
    if mode == "verbatim":
        if result.get("verbatim_statement") != case["actual_output"] or not result.get(
            "text_preserved"
        ):
            raise ValueError("Verbatim result differs from the original field")
        validate_field_score(result["score"], result["outputs"], case["actual_output"])
        statements = [case["actual_output"]]
    elif mode == "extracted":
        validate_score(result["score"], result["outputs"])
        statements = result["outputs"][0]["output"]["statements"]
    else:
        raise ValueError("Unknown scoring unit mode")
    expected = case.get("expected_statements")
    extraction = Counter(expected) == Counter(statements) if expected else None
    if result.get("extraction_matches") != extraction:
        raise ValueError("Saved extraction check differs from the frozen expectation")
    return {
        "status": "extraction_failed" if extraction is False else "scored",
        "score": result["score"],
        "extraction_matches": extraction,
        "extracted_statement_count": len(statements),
    }


def read_jsonl(raw: bytes) -> list[dict]:
    """Use the preserved bytes for parsing and provenance rather than reopening inputs."""
    return [json.loads(line) for line in raw.decode().splitlines() if line.strip()]


def read_export(directory: Path, read: Callable[[Path], bytes]) -> tuple:
    """Verify the complete field inventory before accepting any run's selected subset."""
    manifest = json.loads(read(directory / "manifest.json"))
    for name, digest in manifest["output_hashes"].items():
        if sha256(read(directory / name)).hexdigest() != digest:
            raise ValueError("Export hash mismatch")
    units = read_jsonl(read(directory / "units.jsonl"))
    indexed = {unit["id"]: unit for unit in units}
    coverage = json.loads(read(directory / "coverage.json"))
    inventory = [
        i for packet in coverage for field in packet["fields"] for i in field.get("unit_ids", [])
    ]
    if len(indexed) != len(units) or Counter(inventory) != Counter(indexed.keys()):
        raise ValueError("Export inventory has duplicate or missing units")
    if manifest["unit_count"] != len(units):
        raise ValueError("Export count mismatch")
    return units, indexed, coverage


def reconcile(args: argparse.Namespace) -> dict:
    """Validate export membership and run identities without merging incompatible judgments."""
    snapshots: dict[Path, bytes] = {}

    def read(path: Path) -> bytes:
        """Retain exact bytes and reject changing inputs before producing an aggregate."""
        resolved = path.resolve()
        data = resolved.read_bytes()
        if resolved in snapshots and snapshots[resolved] != data:
            raise ValueError("Evidence changed while reading")
        snapshots[resolved] = data
        return data

    units, indexed, coverage = read_export(args.export, read)
    judged, run_records, identity = {}, [], None
    for spec in json.loads(read(args.runs)):
        inputs = read(Path(spec["cases"]))
        cases = read_jsonl(inputs)
        run = Path(spec["run"])
        summary = json.loads(read(run / "summary.json"))
        if sha256(inputs).hexdigest() != summary["input_sha256"]:
            raise ValueError("Run input hash mismatch")
        if any(indexed.get(case["id"]) != case or case["id"] in judged for case in cases):
            raise ValueError("Cases differ from the export or repeat a scheduled unit")
        current = {key: summary[key] for key in IDENTITY}
        current["unit_mode"] = summary.get("unit_mode", "extracted")
        if identity is not None and current != identity:
            raise ValueError("Model, profile or evaluator sources differ across runs")
        identity = current
        raw = read_jsonl(read(run / "judge-calls.jsonl"))
        judged.update(reconcile_run(cases, summary, raw))
        run_records.append(
            {
                "run": str(run),
                "status": summary["status"],
                "scheduled": len(cases),
                "raw_calls": len(raw),
            }
        )
    rows = [
        {
            "unit_id": unit["id"],
            "case_id": unit["case_id"],
            "field_path": unit["field_path"],
            **judged.get(unit["id"], {"status": "unselected", "score": None}),
        }
        for unit in units
    ]
    if any(path.read_bytes() != data for path, data in snapshots.items()):
        raise ValueError("Evidence changed during reconciliation")
    return {
        "unit_counts": dict(Counter(row["status"] for row in rows)),
        "units": rows,
        "runs": run_records,
        "evaluator": identity,
        "pending_fields": [
            {"case_id": packet["case_id"], **field}
            for packet in coverage
            for field in packet["fields"]
            if field["status"] == "pending_rubric"
        ],
        "whole_packet_scored": False,
        "human_calibrated": False,
        "scope": "Cited-unit faithfulness; within-field extraction completeness remains unproven",
        "input_hashes": {str(path): sha256(data).hexdigest() for path, data in snapshots.items()},
        "checker_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def main() -> None:
    """Write a fresh private coverage report without rewriting any prior score or input."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("export", "runs", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError("Evaluation reports must stay outside the repository")
    report = reconcile(args)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {"unit_counts": report["unit_counts"], "pending_fields": len(report["pending_fields"])}
        )
    )


if __name__ == "__main__":
    main()
