"""Adversarial checks for complete field inventory and exact source context selection."""

import json
import unittest
from unittest.mock import patch

import export_field_units
import test_export_packets as fixtures
from export_field_units import packet_units

from creditlens.citations import cite
from creditlens.domain import Claim, FinancialMetric, Packet
from creditlens.errors import ServiceError
from creditlens.evaluation import GoldCase


class FieldTests(unittest.TestCase):
    """Test export authority and coverage independently from model judgment quality."""

    def setUp(self) -> None:
        """Reuse the canonical interior span fixture, then add cited facts and unscored fields."""
        source = fixtures.ExportTests()
        source.setUp()
        self.page, self.chunk = source.page, source.chunk
        self.pages = {("bank", "doc", "v1", 1): self.page}
        self.case = GoldCase(
            case_id="case",
            gold_version="v1",
            category="calculation",
            question="Calculate debt service coverage.",
            borrower_id="borrower",
            tenant_id="bank",
            principal_subject="analyst",
            principal_role="underwriter",
            borrower_grants=("borrower",),
            acl_groups=("underwriter",),
            effective_at="2026-09-11",
            relevant_pages=(),
            expected_behavior="answer",
            expected_terms=(),
            forbidden_terms=(),
            retrieval_eligible=False,
            answer_rubric="inspect",
        )
        self.packet = Packet(
            request_id="request",
            borrower_id="borrower",
            borrower_summary=(Claim(text=self.chunk.text, citations=(cite(self.chunk),)),),
            calculated_metrics=(
                FinancialMetric(
                    name="DSCR",
                    value="1.5000",
                    unit="ratio",
                    source_fields=("annual_debt_service",),
                    citations=(cite(self.chunk),),
                ),
            ),
            policy_disposition="INSUFFICIENT_EVIDENCE",
            missing_documents=("cash flow",),
            recommended_next_actions=("Collect the missing inputs.",),
            questions_for_underwriter=("Which reporting period applies?",),
            abstained=True,
            evidence=(self.chunk,),
            provider_mode="test",
            corpus_version="test",
            latency_ms=1,
        )

    def export(self, packet: Packet | None = None):
        """Call the real exporter with saved JSON, including revalidation of typed packet values."""
        return packet_units(
            {"outcome": {"packet": (packet or self.packet).model_dump(mode="json")}},
            self.case,
            self.pages,
        )

    def test_every_field_accounted_and_metric_preserved(self) -> None:
        """An omitted metric or pending rubric must be visible before any score is requested."""
        units, ledger = self.export()
        fields = {item["path"]: item for item in ledger["fields"]}
        self.assertEqual(set(fields), {f"/{name}" for name in Packet.model_fields})
        self.assertEqual(len(units), 2)
        self.assertEqual(units[1]["actual_output"], "The calculated DSCR is 1.5000 ratio.")
        self.assertEqual(units[1]["original_value"]["value"], "1.5000")
        self.assertEqual(units[1]["expected_statements"], [units[1]["actual_output"]])
        self.assertEqual(fields["/policy_disposition"]["status"], "pending_rubric")
        self.assertEqual(fields["/missing_documents"]["original_value"], ["cash flow"])
        self.assertEqual(fields["/applicable_policy"]["status"], "empty")
        self.assertFalse(ledger["whole_packet_scored"])
        self.assertEqual(json.loads(units[0]["retrieval_context"][0])["text"], self.chunk.text)

    def test_forged_citation_rejected(self) -> None:
        """An authentic chunk cannot authorize a field's invented page or document tuple."""
        for alteration in ({"page": 2}, {"document_id": "other"}, {"chunk_id": "missing"}):
            citation = cite(self.chunk).model_copy(update=alteration)
            bad = self.packet.model_copy(
                update={"borrower_summary": (Claim(text="Unsupported", citations=(citation,)),)}
            )
            with self.subTest(alteration=alteration), self.assertRaises(ServiceError):
                self.export(bad)

    def test_unsupported_text_stays_in_evaluation(self) -> None:
        """Do not filter a wrong answer out of the denominator when its source identity is valid."""
        bad = self.packet.model_copy(
            update={
                "borrower_summary": (
                    Claim(text="The annual debt service is USD 1.", citations=(cite(self.chunk),)),
                )
            }
        )
        units, _ = self.export(bad)
        self.assertEqual(units[0]["actual_output"], "The annual debt service is USD 1.")
        self.assertIn("120000", units[0]["retrieval_context"][0])

    def test_uncited_evidence_is_not_added_to_context(self) -> None:
        """The union of all retrieved evidence must not conceal a field's irrelevant citation."""
        other = self.chunk.model_copy(
            update={
                "chunk_id": "other-chunk",
                "start_char": 0,
                "end_char": 7,
                "text": self.page.text[:7],
            }
        )
        units, _ = self.export(self.packet.model_copy(update={"evidence": (self.chunk, other)}))
        self.assertEqual(len(units[0]["retrieval_context"]), 1)
        self.assertEqual(json.loads(units[0]["retrieval_context"][0])["chunk_id"], "chunk")

    def test_duplicates_and_schema_drift_rejected(self) -> None:
        """Reject ambiguous evidence identities and schema additions that lack a rubric decision."""
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.export(self.packet.model_copy(update={"evidence": (self.chunk, self.chunk)}))
        with patch.dict(export_field_units.FIELDS, {"unreviewed": "claim"}):
            with self.assertRaisesRegex(ValueError, "schema"):
                self.export()

    def test_repeated_claims_retain_distinct_field_paths(self) -> None:
        """Duplicate content across product fields is retained rather than silently deduplicated."""
        repeated = self.packet.model_copy(
            update={"applicable_policy": self.packet.borrower_summary}
        )
        units, _ = self.export(repeated)
        self.assertEqual(len(units), 3)
        self.assertEqual(len({u["id"] for u in units}), 3)
        self.assertEqual(units[0]["actual_output"], units[2]["actual_output"])


if __name__ == "__main__":
    unittest.main()
