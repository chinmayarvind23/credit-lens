"""Browser packaging must preserve core results while publishing only public synthetic data."""

import pytest

from creditlens.corpus import build_demo_pages
from creditlens.domain import QueryRequest
from creditlens.errors import ServiceError
from creditlens.retrieval import EvidenceCatalog
from creditlens.workflow import QueryWorkflow
from infra.huggingface.browser.bridge import BrowserDemo
from scripts.build_browser_space import public_fixture


@pytest.mark.parametrize("borrower", [f"borrower-{i:03}" for i in range(1, 6)])
@pytest.mark.parametrize(
    "question,date",
    [
        ("Calculate debt service coverage and identify policy exceptions", "2026-06-01"),
        ("What is the minimum DSCR?", "2025-01-01"),
        ("What is the minimum DSCR?", "2026-06-01"),
        ("What is the weather in Tokyo tomorrow?", "2026-06-01"),
    ],
)
def test_browser_matches_server_workflow(borrower: str, question: str, date: str) -> None:
    """Filtering unpublished documents must not change any substantive authorized packet field."""
    demo = BrowserDemo(public_fixture())
    request = QueryRequest(borrower_id=borrower, question=question, effective_at=date)
    browser = demo.dispatch("query", {"request": request.model_dump(mode="json")})
    server = (
        QueryWorkflow(EvidenceCatalog(build_demo_pages()), demo.store)
        .query(request, demo.store.resolve("synthetic-demo"))
        .model_dump(mode="json")
    )
    # The public catalog omits restricted pages, so its corpus fingerprint must differ.
    assert browser["corpus_version"] != server["corpus_version"]
    for packet in (browser, server):
        for field in ("request_id", "latency_ms", "stages", "corpus_version"):
            packet.pop(field)
    assert browser == server
    for chunk in browser["evidence"]:
        assert (
            demo.dispatch(
                "evidence",
                {
                    "request": request.model_dump(mode="json"),
                    "chunk_id": chunk["chunk_id"],
                },
            )
            == chunk
        )


def test_fixture_contains_no_restricted_pages_or_other_borrowers() -> None:
    """Restricted pages must be absent from the download, beyond client-side checks."""
    fixture = public_fixture()
    allowed = {f"borrower-{i:03}" for i in range(1, 6)}
    assert {b["borrower_id"] for b in fixture["borrowers"]} == allowed
    assert fixture["pages"]
    assert all(
        p["tenant_id"] == "demo-bank"
        and p["borrower_id"] in allowed | {None}
        and p["acl_groups"] == ["underwriting"]
        for p in fixture["pages"]
    )
    demo = BrowserDemo(fixture)
    with pytest.raises(ServiceError):
        demo.dispatch(
            "query",
            {
                "request": {
                    "borrower_id": "borrower-006",
                    "question": "Calculate DSCR",
                    "effective_at": "2026-06-01",
                }
            },
        )
    with pytest.raises(ServiceError):
        demo.dispatch(
            "evidence",
            {
                "request": {
                    "borrower_id": "borrower-001",
                    "question": "Calculate DSCR",
                    "effective_at": "2026-06-01",
                },
                "chunk_id": "fabricated-source",
            },
        )
