"""A policy threshold lookup must not require unavailable borrower financial statements."""

import pytest
from fastapi.testclient import TestClient

from creditlens.api import create_app
from creditlens.intent import classify_intent
from creditlens.settings import Settings


@pytest.mark.parametrize(
    "question",
    [
        "What DSCR minimum applies on 2025-06-15?",
        "What is the minimum DSCR?",
        "Explain the policy threshold for debt coverage.",
        "Which threshold governs loan to value?",
    ],
)
def test_policy_references_do_not_require_calculation(question: str) -> None:
    """Generic reference wording must not manufacture a request for borrower arithmetic."""
    assert not classify_intent(question).financial_review


@pytest.mark.parametrize(
    "question",
    [
        "Does this borrower's DSCR meet the minimum?",
        "What minimum applies and calculate coverage from the annual inputs?",
        "Which threshold did the borrower miss?",
        "Is the current coverage covenant met?",
        "Compare cash flow with the policy minimum.",
        "What missing evidence prevents checking the DSCR minimum?",
    ],
)
def test_actual_assessment_still_reaches_finance(question: str) -> None:
    """Reference words cannot suppress explicit calculation or evidence-readiness requests."""
    assert classify_intent(question).financial_review


def test_past_policy_lookup_without_borrower_financials() -> None:
    """The HTTP API returns active policy without abstaining for later borrower documents."""
    with TestClient(create_app(Settings(database_url="sqlite:///:memory:"))) as client:
        response = client.post(
            "/api/v1/query",
            json={
                "borrower_id": "borrower-001",
                "question": "What DSCR minimum applies?",
                "effective_at": "2025-06-15",
            },
        )
        assert response.status_code == 200
        packet = response.json()
        assert packet["policy_disposition"] == "HUMAN_JUDGMENT_REQUIRED"
        assert packet["abstained"] is False
        assert packet["calculated_metrics"] == []
        assert packet["missing_documents"] == []
        assert packet["applicable_policy"]
        assert all(c["document_version"] == "v1" for c in packet["evidence"])
        assert "1.2" in " ".join(c["text"] for c in packet["applicable_policy"])
