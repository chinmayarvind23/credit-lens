"""Check explicit state fields without equating evidence presence with correct behavior."""

import argparse
import json
from hashlib import sha256
from pathlib import Path

FIELDS = {"policy_disposition", "missing_documents", "abstained"}


def check_states(labels: list[dict], records: list[dict]) -> list[dict]:
    """Retain every labeled field, including empty lists and false flags, in the denominator."""
    saved = {row["case_id"]: row for row in records}
    if (
        not labels
        or len(saved) != len(records)
        or len({r["case_id"] for r in labels}) != len(labels)
    ):
        raise ValueError("State inputs need nonempty labels and unique case identities")
    results = []
    for label in labels:
        expected = label["expected_state"]
        if set(expected) != FIELDS or type(expected["abstained"]) is not bool:
            raise ValueError("Every state field requires an explicit typed expectation")
        row = saved[label["case_id"]]
        packet = row.get("packet") or row.get("outcome", {}).get("packet")
        if not isinstance(packet, dict):
            raise ValueError("A labeled state has no saved packet")
        fields = [
            {
                "field": key,
                "expected": value,
                "actual": packet.get(key),
                "passed": key in packet
                and json.dumps(packet[key], sort_keys=True) == json.dumps(value, sort_keys=True),
            }
            for key, value in expected.items()
        ]
        results.append(
            {
                "case_id": label["case_id"],
                "fields": fields,
                "passed": all(field["passed"] for field in fields),
            }
        )
    return results


def main() -> None:
    """Write a fresh report with complete failure details and unchanged input hashes."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("labels", "records", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError("State evidence must remain outside the repository")
    paths = {"labels": args.labels, "records": args.records, "checker": Path(__file__)}
    data = {key: path.read_bytes() for key, path in paths.items()}
    results = check_states(
        *[
            [json.loads(line) for line in data[key].decode().splitlines() if line.strip()]
            for key in ("labels", "records")
        ]
    )
    stable = all(path.read_bytes() == data[key] for key, path in paths.items())
    fields = [field for result in results for field in result["fields"]]
    report = {
        "contract": "explicit-packet-state-v1",
        "case_count": len(results),
        "passed_cases": sum(r["passed"] for r in results),
        "field_count": len(fields),
        "passed_fields": sum(f["passed"] for f in fields),
        "results": results,
        "input_hashes": {key: sha256(raw).hexdigest() for key, raw in data.items()},
        "provenance_stable": stable,
        "scope": "Operational state, not semantic quality",
    }
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, indent=2) + "\n")
    print(
        f"State fields: {report['passed_fields']}/{len(fields)}; "
        f"cases: {report['passed_cases']}/{len(results)}"
    )
    raise SystemExit(0 if stable and all(r["passed"] for r in results) else 1)


if __name__ == "__main__":
    main()
