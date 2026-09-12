"""Population accounting rejects silent omissions and altered frozen model inputs."""

import json
import unittest
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from export_population import population
from run_population import read_population
from test_field_units import FieldTests


class PopulationTests(unittest.TestCase):
    """Exercise real canonical export while keeping inference quality out of protocol tests."""

    def setUp(self):
        """Reuse a valid packet with a cited claim and metric, then vary population outcomes."""
        self.fixture = FieldTests()
        self.fixture.setUp()

    def test_all_cases_retained_including_missing_and_denied(self):
        """The population denominator must never become just the successfully exported subset."""
        f = self.fixture
        cases = [f.case.model_copy(update={"case_id": str(i)}) for i in range(4)]
        records = [
            {"case_id": "0", "outcome": {"packet": f.packet.model_dump(mode="json")}},
            {"case_id": "1", "outcome": {"denied": True, "fixture_pass": True}},
            {"case_id": "2", "outcome": {}},
        ]
        units, ledger = population(cases, [f.page], records)
        self.assertEqual(len(units), 2)
        self.assertEqual(
            [row["status"] for row in ledger],
            ["exported", "denied", "missing_packet", "missing_record"],
        )
        self.assertFalse(ledger[0]["whole_packet_scored"])

    def test_forged_packet_remains_visible_without_becoming_judge_context(self):
        """An invalid citation is counted as an invalid packet, never silently omitted."""
        f = self.fixture
        packet = f.packet.model_dump(mode="json")
        packet["borrower_summary"][0]["citations"][0]["page"] = 2
        units, ledger = population(
            [f.case], [f.page], [{"case_id": f.case.case_id, "outcome": {"packet": packet}}]
        )
        self.assertEqual(units, [])
        self.assertEqual(ledger[0]["status"], "invalid_packet")

    def test_duplicate_and_unknown_record_identities_fail(self):
        """Neither duplicate overwrites nor unrelated saved outputs may change the population."""
        f = self.fixture
        for records in ([{"case_id": "unknown"}], [{"case_id": f.case.case_id}] * 2):
            with self.assertRaises(ValueError):
                population([f.case], [f.page], records)

    def test_frozen_shard_and_ledger_tampering_fail(self):
        """The runner verifies both its model inputs and the ungraded-case ledger."""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            shard = b'{"id": "unit"}\n'
            ledger = b"[]"
            manifest = {
                "unit_count": 1,
                "coverage_sha256": sha256(ledger).hexdigest(),
                "shards": [
                    {"file": "shard.jsonl", "sha256": sha256(shard).hexdigest(), "ids": ["unit"]}
                ],
            }
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (root / "coverage.json").write_bytes(ledger)
            (root / "shard.jsonl").write_bytes(shard)
            self.assertEqual(read_population(root)[1], [{"id": "unit"}])
            (root / "shard.jsonl").write_bytes(b'{"id":"changed"}')
            with self.assertRaises(ValueError):
                read_population(root)
            (root / "shard.jsonl").write_bytes(shard)
            (root / "coverage.json").write_bytes(b"[{}]")
            with self.assertRaises(ValueError):
                read_population(root)


if __name__ == "__main__":
    unittest.main()
