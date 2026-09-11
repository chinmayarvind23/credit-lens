"""Validate authored labels independently of any retrieval implementation."""

import json
from collections import Counter
from datetime import date
from pathlib import Path

from creditlens.corpus import borrower_pages, policy_pages


def test_frozen_gold_has_required_balance_and_only_authorized_positive_qrels() -> None:
    """Security negatives cannot become relevant labels that reward unauthorized retrieval."""
    source = Path(__file__).resolve().parents[1] / "evals" / "gold_cases.jsonl"
    cases = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
    assert len(cases) == len({case["case_id"] for case in cases}) == 240
    assert Counter(case["category"] for case in cases) == {
        "direct_policy_lookup": 60,
        "borrower_evidence": 40,
        "structured_calculation": 30,
        "multi_document_synthesis": 25,
        "missing_documents": 20,
        "policy_exceptions": 20,
        "contradictory_evidence": 15,
        "acl_tenant_isolation": 10,
        "prompt_injection_adversarial": 10,
        "stale_versioned_policy": 10,
    }
    pages = policy_pages() + tuple(
        page for number in range(1, 201) for page in borrower_pages(number)
    )
    lookup = {(page.document_id, page.document_version, page.page): page for page in pages}
    for case in cases:
        assert case["principal_subject"]
        assert case["principal_role"] == "underwriter"
        assert case["answer_rubric"]
        effective = date.fromisoformat(case["effective_at"])
        for qrel in case["relevant_pages"]:
            page = lookup[(qrel["document_id"], qrel["document_version"], qrel["page"])]
            assert page.tenant_id == case["tenant_id"]
            assert page.borrower_id in (None, case["borrower_id"])
            assert page.borrower_id is None or page.borrower_id in case["borrower_grants"]
            assert set(page.acl_groups) & set(case["acl_groups"])
            assert page.valid_from <= effective
            assert page.valid_to is None or effective < page.valid_to
        if case["category"] in ("acl_tenant_isolation", "prompt_injection_adversarial"):
            assert not case["retrieval_eligible"]
            assert not case["relevant_pages"]
