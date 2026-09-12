"""Export unchanged guidance fields with canonical evidence and separately authored expectations."""

import argparse
import json
from hashlib import sha256
from pathlib import Path

from export_packets import export_case

from creditlens.evaluation import page_key, read_inputs

FIELDS = ("recommended_next_actions", "questions_for_underwriter")


def export(args: argparse.Namespace) -> None:
    """Keep every selected field, including empty lists, and verify its original source context."""
    repo = Path(__file__).resolve().parents[2]
    if args.output.resolve().is_relative_to(repo):
        raise ValueError("Advice exports must stay outside the repository")
    paths = {name: getattr(args, name) for name in ("gold", "pages", "records", "labels")}
    paths.update(
        {
            "exporter": Path(__file__),
            "packet_exporter": Path(__file__).with_name("export_packets.py"),
            "domain": repo / "src/creditlens/domain.py",
            "scope": repo / "src/creditlens/evaluation.py",
        }
    )
    before = {key: sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}
    cases, pages = read_inputs(args.gold, args.pages)
    gold = {case.case_id: case for case in cases}
    canonical = {(page.tenant_id, *page_key(page)): page for page in pages}
    labels = [json.loads(line) for line in args.labels.read_text(encoding="utf-8").splitlines()]
    records = [json.loads(line) for line in args.records.read_text(encoding="utf-8").splitlines()]
    saved = {row["case_id"]: row for row in records}
    if len(saved) != len(records) or len(gold) != len(cases) or len(canonical) != len(pages):
        raise ValueError("Advice inputs have duplicate identities")
    if not 1 <= len(labels) <= 12 or len({r["case_id"] for r in labels}) != len(labels):
        raise ValueError("Select one to twelve unique packets, with two fields each")
    units = []
    for label in labels:
        identity = label["case_id"]
        if not isinstance(label.get("guidance"), str) or not label["guidance"].strip():
            raise ValueError("Each packet needs explicit guidance expectations")
        verified = export_case(saved[identity], gold[identity], canonical)
        packet = json.loads(verified["actual_output"])
        for field in FIELDS:
            units.append(
                {
                    "id": f"{identity}/{field}",
                    "case_id": identity,
                    "field_path": f"/{field}",
                    "original_value": packet[field],
                    "input": f"Field: {field}\n{verified['input']}",
                    "actual_output": json.dumps(packet[field], ensure_ascii=False),
                    "expected_output": label["guidance"],
                    "retrieval_context": verified["retrieval_context"],
                }
            )
    if before != {key: sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}:
        raise ValueError("Advice inputs changed during export")
    payload = "".join(json.dumps(unit, ensure_ascii=False) + "\n" for unit in units).encode()
    manifest = {
        "input_hashes": before,
        "unit_count": len(units),
        "unit_ids": [unit["id"] for unit in units],
        "cases_sha256": sha256(payload).hexdigest(),
        "fields": FIELDS,
        "selection": "Every guidance field in the explicitly labeled packets",
        "scope": "Source-grounded advice appropriateness, not factual faithfulness",
    }
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "cases.jsonl").write_bytes(payload)
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Exported {len(units)} original guidance fields with verified source context")


def main() -> None:
    """Require explicit source records and frozen expectations; never regenerate an answer."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("gold", "pages", "records", "labels", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    export(parser.parse_args())


if __name__ == "__main__":
    main()
