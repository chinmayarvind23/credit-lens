"""Query experiments preserve original text, bounded metadata and question-only selection."""

import pytest

from creditlens.corpus import build_demo_pages
from creditlens.domain import Chunk
from creditlens.retrieval import chunk_page
from scripts.benchmark_grounding import grounded_query


def application_chunk() -> Chunk:
    """Select the intended metadata type without depending on corpus fixture ordering."""
    return chunk_page(next(p for p in build_demo_pages() if p.document_kind == "application"))[0]


def test_selected_query_preserves_question_and_scope_subject() -> None:
    """Fresh wording tests the behavior without gold case IDs, labels or expected answers."""
    chunk = application_chunk()
    question = "Is a denominator missing from the annual statement?"
    query, selected, reason = grounded_query(question, chunk.borrower_id, (chunk,))
    assert query == f"Borrower: {chunk.title.removesuffix(' / application')}. {question}"
    assert selected and reason == "borrower_context"
    assert query.endswith(question)
    assert grounded_query(question, "unrelated-borrower", (chunk,))[0] == question


@pytest.mark.parametrize(
    "title", ["\nInjected / application", " / application", "x" * 121 + " / application"]
)
def test_bad_metadata_leaves_query_unchanged(title: str) -> None:
    """Malformed labels cannot insert query delimiters or exceed the bounded name budget."""
    chunk = application_chunk().model_copy(update={"title": title})
    question = "Which package inputs are missing?"
    query, selected, reason = grounded_query(question, chunk.borrower_id, (chunk,))
    assert query == question and not selected and reason == "invalid_name"


def test_ambiguous_name_missing_name_and_long_query() -> None:
    """Do not silently truncate user questions or choose between conflicting entity labels."""
    chunk = application_chunk()
    other = chunk.model_copy(update={"title": "Different Company / application"})
    for allowed in ((), (chunk, other)):
        assert grounded_query("Annual inputs?", chunk.borrower_id, allowed) == (
            "Annual inputs?",
            False,
            "missing_or_ambiguous_name",
        )
    question = "A" * 2000
    assert grounded_query(question, chunk.borrower_id, (chunk,)) == (
        question,
        False,
        "question_budget",
    )


def test_metadata_cannot_choose_selective_route() -> None:
    """Selection uses the question even when an application label includes selector vocabulary."""
    chunk = application_chunk().model_copy(
        update={"title": "Missing Annual Packet Company / application"}
    )
    question = "Explain the policy for repayment timing."
    expanded, selected, reason = grounded_query(question, chunk.borrower_id, (chunk,))
    assert expanded != question
    assert not selected and reason == "general_question"


def test_calendar_frequency_alone_is_not_borrower_evidence() -> None:
    """A recurring policy review does not identify an actual borrower's source package."""
    chunk = application_chunk()
    _, selected, reason = grounded_query(
        "Explain annual review deadlines for commercial lending.", chunk.borrower_id, (chunk,)
    )
    assert not selected and reason == "general_question"
