"""Grade explicit policy-reference fields separately from broad historical fixture checks."""

import argparse
import json
from hashlib import sha256
from pathlib import Path
from typing import Any

FIELDS = {"policy_disposition", "abstained", "calculated_metrics", "missing_documents"}


def check_rows(labels: list[dict], records: list[dict]) -> list[dict[str, Any]]:
    """Retain every labeled field; evidence alone cannot mask false abstention."""
    saved = {r["case_id"]: r for r in records}
    if len(saved) != len(records) or len({r["case_id"] for r in labels}) != len(labels):
        raise ValueError("Duplicate case identities")
    if not labels:
        raise ValueError("Field expectations cannot be empty")
    results = []
    for label in labels:
        expected = label["expected_fields"]
        if set(expected) != FIELDS:
            raise ValueError("Every policy field must have an explicit expectation")
        record = saved[label["case_id"]]
        packet = record.get("packet") or record.get("outcome", {}).get("packet")
        if not isinstance(packet, dict):
            raise ValueError("A labeled policy case has no saved packet")
        actual = {key: packet.get(key) for key in expected}
        fields = {key: key in packet and actual[key] == expected[key] for key in expected}
        policy = bool(packet.get("applicable_policy"))
        results.append(
            {
                "case_id": label["case_id"],
                "expected_fields": expected,
                "actual_fields": actual,
                "field_checks": fields,
                "policy_evidence_present": policy,
                "passed": all(fields.values()) and policy,
            }
        )
    return results


def main() -> None:
    """Write a fresh private report, including failures and hashes of the exact saved inputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("labels", "records", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError("Evaluation evidence must stay outside the repository")
    paths = {"labels": args.labels, "records": args.records, "checker": Path(__file__)}
    inputs = {name: path.read_bytes() for name, path in paths.items()}
    rows = check_rows(
        [json.loads(line) for line in inputs["labels"].decode().splitlines() if line.strip()],
        [json.loads(line) for line in inputs["records"].decode().splitlines() if line.strip()],
    )
    stable = all(path.read_bytes() == inputs[name] for name, path in paths.items())
    passed = sum(row["passed"] for row in rows)
    report = {
        "contract": "policy-reference-fields-v1",
        "passed_cases": passed,
        "total_cases": len(rows),
        "source_hashes": {name: sha256(data).hexdigest() for name, data in inputs.items()},
        "provenance_stable": stable,
        "results": rows,
        "scope": "Explicit policy-reference state fields; not semantic groundedness",
    }
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, indent=2) + "\n")
    print(f"Policy reference fields: {passed}/{len(rows)} cases passed; stable={stable}")
    raise SystemExit(0 if passed == len(rows) and stable else 1)


if __name__ == "__main__":
    main()
