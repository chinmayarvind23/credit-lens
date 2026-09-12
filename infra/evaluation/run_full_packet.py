"""Freeze all complete packets and run bounded GEval batches with a full-case coverage ledger."""

import argparse
import json
from hashlib import sha256
from pathlib import Path

from export_packets import FACT_FIELDS, export_case
from run_advice_eval import run as run_batch

from creditlens.evaluation import page_key, read_inputs

BOUNDARY = (
    "Assess the actual question against the whole packet and canonical context. Do not infer "
    "a positive label from this guidance. Unsupported, irrelevant, materially incomplete or "
    "unsafe responses fail; appropriate evidence-based refusals can pass."
)


def digest(path):
    """Bind evidence to exact bytes rather than a mutable output directory name."""
    return sha256(path.read_bytes()).hexdigest()


def export(gold, pages, records, output):
    """Export every authored case before grading; denials and invalid outputs stay visible."""
    repo = Path(__file__).resolve().parents[2]
    if output.exists() or output.resolve().is_relative_to(repo):
        raise ValueError("Choose a fresh private population export")
    sources = {"gold": gold, "pages": pages, "records": records}
    sources.update({p.name: p for p in Path(__file__).parent.glob("*.py")})
    before = {key: digest(path) for key, path in sources.items()}
    cases, canonical = read_inputs(gold, pages)
    lookup = {(p.tenant_id, *page_key(p)): p for p in canonical}
    saved = [json.loads(line) for line in records.read_text(encoding="utf-8").splitlines()]
    by_id = {r["case_id"]: r for r in saved}
    if (
        len(lookup) != len(canonical)
        or len(by_id) != len(saved)
        or set(by_id) != {c.case_id for c in cases}
    ):
        raise ValueError("Complete unique canonical and case identities are required")
    units, ledger = [], []
    for case in cases:
        record = by_id[case.case_id]
        outcome = record.get("outcome") or {}
        row = {"id": case.case_id, "category": case.category}
        if outcome.get("packet") is None:
            row.update(
                status="denied" if outcome.get("denied") else "missing_packet",
                fixture_pass=outcome.get("fixture_pass"),
            )
        else:
            unit = export_case(record, case, lookup)
            unit["expected_output"] = BOUNDARY
            units.append(unit)
            row["status"] = "exported"
        ledger.append(row)
    if before != {key: digest(path) for key, path in sources.items()}:
        raise ValueError("Source changed during export")
    output.mkdir(parents=True)
    batches = []
    for start in range(0, len(units), 24):
        path = output / f"batch-{start // 24:03d}.jsonl"
        path.write_text(
            "".join(json.dumps(u) + "\n" for u in units[start : start + 24]), encoding="utf-8"
        )
        batches.append(
            {
                "file": path.name,
                "sha256": digest(path),
                "ids": [u["id"] for u in units[start : start + 24]],
            }
        )
    manifest = {
        "source_hashes": before,
        "case_count": len(cases),
        "packet_count": len(units),
        "ledger": ledger,
        "batches": batches,
        "fields": FACT_FIELDS,
        "metric": "whole-packet-lending-v1",
        "human_calibrated": False,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"cases": len(cases), "packets": len(units), "batches": len(batches)}))


def run(args):
    """Attempt every frozen packet; retain failed controls and ungraded cases."""
    repo = Path(__file__).resolve().parents[2]
    if args.output.exists() or args.output.resolve().is_relative_to(repo):
        raise ValueError("Choose a fresh private run directory")
    manifest_path = args.population / "manifest.json"
    population = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = [args.population / b["file"] for b in population["batches"]]
    for path, batch in zip(paths, population["batches"], strict=True):
        if digest(path) != batch["sha256"]:
            raise ValueError("Frozen packet batch changed")
    frozen = {str(p.resolve()): digest(p) for p in [manifest_path, args.controls, *paths]}
    evaluator_paths = [*Path(__file__).parent.glob("*.py"), Path(__file__).with_name("uv.lock")]
    frozen.update({str(p.resolve()): digest(p) for p in evaluator_paths})
    args.output.mkdir(parents=True)
    summary = {
        "status": "running",
        "population_sha256": digest(manifest_path),
        "input_hashes": frozen,
        "batches": [],
        "human_calibrated": False,
        "metric": "whole-packet-lending-v1",
    }
    try:
        for index, path in enumerate([args.controls, *paths]):
            directory = args.output / ("controls" if index == 0 else f"batch-{index - 1:03d}")
            item = {"cases": str(path.resolve()), "run": str(directory.resolve())}
            summary["batches"].append(item)
            try:
                run_batch(
                    argparse.Namespace(
                        cases=path,
                        output=directory,
                        model=args.model,
                        digest=args.digest,
                        rubric="whole-packet-lending-v1",
                    )
                )
                item["status"] = "completed"
            except Exception as error:
                item.update(status="failed", error_type=type(error).__name__)
            item["artifact_hashes"] = {
                p.name: digest(p) for p in directory.glob("*") if p.is_file()
            }
            (args.output / "summary.json").write_text(
                json.dumps(summary, indent=2), encoding="utf-8"
            )
            print(json.dumps({"batch": directory.name, "status": item["status"]}), flush=True)
            if index >= 3 and all(x["status"] == "failed" for x in summary["batches"][-3:]):
                break
        summary["status"] = (
            "completed"
            if len(summary["batches"]) == len(paths) + 1
            and all(x["status"] == "completed" for x in summary["batches"])
            else "failed"
        )
    finally:
        summary["input_stable"] = all(digest(Path(p)) == h for p, h in frozen.items())
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if not summary["input_stable"] or summary["status"] != "completed":
        raise RuntimeError("Whole-packet run did not finish with stable inputs")


def main():
    """Separate immutable export from inference so inputs can be inspected before execution."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("export")
    for name in ("gold", "pages", "records", "output"):
        prepare.add_argument(f"--{name}", type=Path, required=True)
    execute = commands.add_parser("run")
    for name in ("population", "controls", "output"):
        execute.add_argument(f"--{name}", type=Path, required=True)
    for name in ("model", "digest"):
        execute.add_argument(f"--{name}", required=True)
    args = parser.parse_args()
    if args.command == "export":
        export(args.gold, args.pages, args.records, args.output)
    else:
        run(args)


if __name__ == "__main__":
    main()
