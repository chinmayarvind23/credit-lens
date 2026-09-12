"""Operational checks must expose false abstention and fabricated missing evidence."""

import unittest

from check_packet_state import check_states


class PacketStateTests(unittest.TestCase):
    """Keep empty and false expected values, and reject missing or duplicate case identities."""

    def test_false_missing_fields_and_coerced_flags_fail(self) -> None:
        """Evidence presence cannot hide bad state, and false is distinct from numeric zero."""
        expected = {
            "policy_disposition": "HUMAN_JUDGMENT_REQUIRED",
            "abstained": False,
            "missing_documents": [],
        }
        labels = [{"case_id": "reference", "expected_state": expected}]
        good = [{"case_id": "reference", "packet": expected}]
        self.assertTrue(check_states(labels, good)[0]["passed"])
        for bad in (
            {**expected, "abstained": True},
            {**expected, "abstained": 0},
            {**expected, "missing_documents": ["annual_debt_service"]},
        ):
            self.assertFalse(
                check_states(labels, [{"case_id": "reference", "packet": bad}])[0]["passed"]
            )
        with self.assertRaises(KeyError):
            check_states(labels, [])
        with self.assertRaises(ValueError):
            check_states(labels * 2, good)


if __name__ == "__main__":
    unittest.main()
