"""Freeze every benchmark case and its exact cited fields without selecting successful packets."""

import argparse
import json
from collections import Counter
from hashlib import sha256
from pathlib import Path

from export_field_units import packet_units

from creditlens.errors import ServiceError
from creditlens.evaluation import page_key, read_inputs


def population(cases, pages, records):
    """Retain invalid and absent outputs in the denominator while excluding them from judging."""
    canonical = {(p.tenant_id, *page_key(p)): p for p in pages}
    saved = {r["case_id"]: r for r in records}
    identities = {c.case_id for c in cases}
    if len(saved) != len(records) or len(canonical) != len(pages) or len(identities) != len(cases):
        raise ValueError("Population identities must be unique")
    if set(saved) - identities:
        raise ValueError("Saved outputs contain unknown benchmark cases")
    units, ledger = [], []
    for case in cases:
        row = {"case_id": case.case_id, "category": case.category, "status": "missing_record"}
        record = saved.get(case.case_id)
        if record is not None:
            outcome = record.get("outcome") or {}
            if outcome.get("packet") is None:
                row["status"] = "denied" if outcome.get("denied") else "missing_packet"
                row["fixture_pass"] = outcome.get("fixture_pass")
            else:
                try:
                    emitted, fields = packet_units(record, case, canonical)
                except (ValueError, KeyError, TypeError, ServiceError) as error:
                    row.update(status="invalid_packet", error_type=type(error).__name__)
                else:
                    units.extend(emitted)
                    row.update(status="exported", **fields)
        ledger.append(row)
    return units, ledger


def export(args):
    """Write fresh immutable shards, provenance and a ledger covering the complete gold set."""
    repo = Path(__file__).resolve().parents[2]
    if args.output.exists() or args.output.resolve().is_relative_to(repo):
        raise ValueError("Use a fresh output directory outside the repository")
    paths = {"gold": args.gold, "pages": args.pages, "records": args.records}
    paths.update({str(p.relative_to(repo)): p for p in (repo / "src/creditlens").glob("*.py")})
    paths.update({p.name: p for p in Path(__file__).parent.glob("*.py")})
    before = {key: sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}
    cases, pages = read_inputs(args.gold, args.pages)
    records = [
        json.loads(line)
        for line in args.records.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    units, ledger = population(cases, pages, records)
    if before != {key: sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}:
        raise ValueError("Population inputs changed during export")
    args.output.mkdir(parents=True)
    shards = []
    for start in range(0, len(units), 24):
        name = f"shard-{start // 24:04d}.jsonl"
        content = "".join(
            json.dumps(unit, ensure_ascii=False) + "\n" for unit in units[start : start + 24]
        ).encode("utf-8")
        (args.output / name).write_bytes(content)
        shards.append(
            {
                "file": name,
                "sha256": sha256(content).hexdigest(),
                "ids": [unit["id"] for unit in units[start : start + 24]],
            }
        )
    ledger_bytes = (json.dumps(ledger, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    (args.output / "coverage.json").write_bytes(ledger_bytes)
    manifest = {
        "schema_version": 1,
        "case_count": len(cases),
        "unit_count": len(units),
        "case_statuses": dict(Counter(row["status"] for row in ledger)),
        "input_hashes": before,
        "shards": shards,
        "coverage_sha256": sha256(ledger_bytes).hexdigest(),
        "whole_packet_scored": False,
        "human_calibrated": False,
        "scope": "Every gold case retained; exact cited fields only are model inputs",
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({key: manifest[key] for key in ("case_count", "unit_count", "case_statuses")}))


def main():
    """Require the original benchmark inputs; no sampling or answer regeneration is allowed."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("gold", "pages", "records", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    export(parser.parse_args())


if __name__ == "__main__":
    main()
