"""Stricter field expectations must expose false abstention despite otherwise valid evidence."""

import unittest

from check_policy_fields import check_rows


class PolicyFieldTests(unittest.TestCase):
    """Check the new field denominator independently from the historical broad fixture rubric."""

    def test_evidence_cannot_hide_wrong_state(self) -> None:
        """A policy excerpt cannot compensate for a false missing-input or abstention state."""
        expected = {
            "policy_disposition": "HUMAN_JUDGMENT_REQUIRED",
            "abstained": False,
            "calculated_metrics": [],
            "missing_documents": [],
        }
        labels = [{"case_id": "case", "expected_fields": expected}]
        packet = {**expected, "applicable_policy": [{"text": "Minimum DSCR is 1.25."}]}
        self.assertTrue(check_rows(labels, [{"case_id": "case", "packet": packet}])[0]["passed"])
        bad = {**packet, "abstained": True, "missing_documents": ["annual_debt_service"]}
        result = check_rows(labels, [{"case_id": "case", "packet": bad}])[0]
        self.assertFalse(result["passed"])
        self.assertFalse(result["field_checks"]["abstained"])
        self.assertFalse(result["field_checks"]["missing_documents"])
        with self.assertRaises(KeyError):
            check_rows(labels, [])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            check_rows(labels * 2, [{"case_id": "case", "packet": packet}])


if __name__ == "__main__":
    unittest.main()
