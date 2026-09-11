"""Prevent omitted, duplicated or incompatible judgments from inflating field coverage."""

import argparse
import copy
import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from reconcile_field_runs import IDENTITY, reconcile, reconcile_run


def fixture() -> tuple[list, dict, list]:
    """Use protocol fixtures to test accounting, without presenting them as model judgments."""
    cases = [{"id": "case/claim/0", "case_id": "case", "field_path": "/claim/0"}]
    outputs = [
        {"schema": "StatementGeneratorOutput", "output": {"statements": ["Supported fact."]}},
        {
            "schema": "NLIStatementOutput",
            "output": {
                "statements": [
                    {"statement": "Supported fact.", "verdict": 1, "reason": "In the source."}
                ]
            },
        },
    ]
    summary = {
        "status": "completed",
        "provenance_stable": True,
        "case_count": 1,
        "results": [
            {
                "id": cases[0]["id"],
                "score": 1.0,
                "structure_valid": True,
                "extraction_matches": None,
                "outputs": outputs,
            }
        ],
    }
    raw = [
        {
            "response": {
                "done": True,
                "done_reason": "stop",
                "message": {"content": json.dumps(o["output"])},
            }
        }
        for o in outputs
    ]
    return cases, summary, raw


class ReconciliationTests(unittest.TestCase):
    """Exercise missing work and corrupted provenance as well as complete accounting."""

    def test_failed_prefix_retains_failed_and_unrun_units(self) -> None:
        """A failed generation cannot remove itself or subsequent cases from the denominator."""
        cases, summary, raw = fixture()
        cases.extend([{"id": "second"}, {"id": "third"}])
        summary.update(status="failed", case_count=3)
        summary["results"].append({"id": "second", "outputs": []})
        raw.append({"response": {"message": {"content": "incomplete"}}})
        ledger = reconcile_run(cases, summary, raw)
        self.assertEqual([r["status"] for r in ledger.values()], ["scored", "failed", "unrun"])

    def test_missing_duplicate_and_out_of_order_results_fail(self) -> None:
        """The saved result sequence must match the complete run or its exact failed prefix."""
        cases, summary, raw = fixture()
        for mutation in ("missing", "duplicate", "wrong_identity", "running", "unstable"):
            changed = copy.deepcopy(summary)
            if mutation == "missing":
                changed["results"] = []
            elif mutation == "duplicate":
                changed["results"] *= 2
            elif mutation == "wrong_identity":
                changed["results"][0]["id"] = "different"
            elif mutation == "running":
                changed["status"] = "running"
            else:
                changed["provenance_stable"] = False
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                reconcile_run(cases, changed, raw)

    def test_raw_output_and_score_corruption_fail(self) -> None:
        """A summary score and its raw journal must agree with the retained verdicts."""
        cases, summary, raw = fixture()
        bad_raw = copy.deepcopy(raw)
        bad_raw[0]["response"]["message"]["content"] = '{"statements": ["Different."]}'
        with self.assertRaisesRegex(ValueError, "raw model"):
            reconcile_run(cases, summary, bad_raw)
        summary["results"][0]["score"] = 0.0
        with self.assertRaisesRegex(ValueError, "score"):
            reconcile_run(cases, summary, raw)

    def test_extraction_failure_is_not_scored_coverage(self) -> None:
        """A valid support score cannot hide omission of a frozen expected statement."""
        cases, summary, raw = fixture()
        cases[0]["expected_statements"] = ["Missing fact."]
        summary["status"] = "failed"
        summary["results"][0]["extraction_matches"] = False
        row = reconcile_run(cases, summary, raw)[cases[0]["id"]]
        self.assertEqual(row["status"], "extraction_failed")
        summary["results"][0]["extraction_matches"] = True
        with self.assertRaisesRegex(ValueError, "extraction check"):
            reconcile_run(cases, summary, raw)

    def test_verbatim_mode_requires_one_exact_original_field(self) -> None:
        """One raw NLI response counts only when it covers the exact preserved field."""
        cases, summary, raw = fixture()
        cases[0]["actual_output"] = "Supported fact."
        summary["unit_mode"] = "verbatim"
        result = summary["results"][0]
        result.update(verbatim_statement="Supported fact.", text_preserved=True)
        result["outputs"] = result["outputs"][1:]
        row = reconcile_run(cases, summary, raw[1:])[cases[0]["id"]]
        self.assertEqual(row["status"], "scored")
        self.assertEqual(row["extracted_statement_count"], 1)
        cases[0]["actual_output"] += " Unsupported addition."
        with self.assertRaisesRegex(ValueError, "original field"):
            reconcile_run(cases, summary, raw[1:])

    def test_aggregate_checks_membership_hashes_and_remaining_fields(self) -> None:
        """Hash-valid files still cannot repeat a unit or silently drop the export inventory."""
        cases, summary, raw = fixture()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            cases_bytes = (json.dumps(cases[0]) + "\n").encode()
            coverage = [
                {
                    "case_id": "case",
                    "fields": [
                        {"status": "exported", "unit_ids": [cases[0]["id"]]},
                        {"path": "/abstained", "status": "pending_rubric", "original_value": False},
                    ],
                }
            ]
            files = {"units.jsonl": cases_bytes, "coverage.json": json.dumps(coverage).encode()}
            for path, data in files.items():
                (root / path).write_bytes(data)
            manifest = {
                "unit_count": 1,
                "output_hashes": {path: sha256(data).hexdigest() for path, data in files.items()},
            }
            (root / "manifest.json").write_text(json.dumps(manifest))
            summary.update({key: "fixed" for key in IDENTITY})
            summary["input_sha256"] = sha256(cases_bytes).hexdigest()
            (root / "summary.json").write_text(json.dumps(summary))
            (root / "judge-calls.jsonl").write_text("\n".join(map(json.dumps, raw)))
            spec = {"cases": str(root / "units.jsonl"), "run": str(root)}
            (root / "runs.json").write_text(json.dumps([spec]))
            args = argparse.Namespace(export=root, runs=root / "runs.json")
            report = reconcile(args)
            self.assertEqual(report["unit_counts"], {"scored": 1})
            self.assertEqual(len(report["pending_fields"]), 1)
            self.assertFalse(report["whole_packet_scored"])
            (root / "runs.json").write_text(json.dumps([spec, spec]))
            with self.assertRaisesRegex(ValueError, "repeat"):
                reconcile(args)
            (root / "runs.json").write_text(json.dumps([spec]))
            (root / "units.jsonl").write_text("corrupt")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                reconcile(args)


if __name__ == "__main__":
    unittest.main()
