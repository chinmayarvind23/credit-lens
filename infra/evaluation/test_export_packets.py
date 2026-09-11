"""Adversarial checks for the boundary between saved answers and canonical judge evidence."""

import unittest
from hashlib import sha256

from export_packets import export_case, verify_chunk

from creditlens.domain import Chunk, Page
from creditlens.evaluation import GoldCase


class ExportTests(unittest.TestCase):
    """A forged saved chunk must not become a judge's trusted source context."""

    def setUp(self) -> None:
        """Use an interior page span so accidental full-page equality checks cannot pass."""
        self.page = Page(
            tenant_id="bank",
            borrower_id="borrower",
            document_id="doc",
            document_version="v1",
            page=1,
            document_kind="financial",
            title="Evidence",
            section="finance",
            text="prefix: annual debt service is USD 120000. suffix",
            acl_groups=("underwriter",),
            valid_from="2026-01-01",
            content_hash=sha256(b"canonical").hexdigest(),
            parser_version="test",
        )
        self.chunk = Chunk(
            **{
                **self.page.model_dump(),
                "text": self.page.text[8:41],
                "start_char": 8,
                "end_char": 41,
                "chunk_id": "chunk",
                "chunker_version": "test",
            }
        )

    def test_canonical_span_accepted(self) -> None:
        """Preserve the exact retrieved span, not additional unseen page text."""
        verify_chunk(self.chunk, self.page)

    def test_altered_evidence_rejected(self) -> None:
        """Reject text substitution and forged scope, provenance and offsets independently."""
        for alteration in (
            {"text": "annual debt service is USD 1"},
            {"acl_groups": ("public",)},
            {"content_hash": "forged"},
            {"valid_from": "2025-01-01"},
            {"end_char": 500},
            {"start_char": 9},
        ):
            with self.subTest(alteration=alteration), self.assertRaises(ValueError):
                verify_chunk(self.chunk.model_copy(update=alteration), self.page)

    def test_unauthorized_packet_rejected(self) -> None:
        """A page can be authentic yet forbidden to the evaluated user's authored scope."""
        case = GoldCase(
            case_id="case",
            gold_version="v1",
            category="acl",
            question="Debt service?",
            borrower_id="borrower",
            tenant_id="bank",
            principal_subject="analyst",
            principal_role="underwriter",
            borrower_grants=("borrower",),
            acl_groups=("other",),
            effective_at="2026-09-11",
            relevant_pages=(),
            expected_behavior="deny",
            expected_terms=(),
            forbidden_terms=(),
            retrieval_eligible=False,
            answer_rubric="deny",
        )
        record = {
            "outcome": {
                "packet": {
                    "request_id": "request",
                    "borrower_id": "borrower",
                    "policy_disposition": "INSUFFICIENT_EVIDENCE",
                    "abstained": True,
                    "evidence": [self.chunk.model_dump()],
                    "provider_mode": "test",
                    "corpus_version": "test",
                    "latency_ms": 1,
                }
            }
        }
        with self.assertRaisesRegex(ValueError, "authored scope"):
            export_case(record, case, {})


if __name__ == "__main__":
    unittest.main()
