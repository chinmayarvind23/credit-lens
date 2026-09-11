"""Inspectable lending intent rules never use borrower identities or retrieved instructions."""

import re
from dataclasses import dataclass

from creditlens.domain import Chunk
from creditlens.retrieval import terms

FINANCIAL_SUBJECTS = frozenset(
    "dscr coverage financial underwriting packet exception exceptions debt repayment repayments "
    "principal interest cash ratio".split()
)
EVIDENCE_REVIEW = frozenset(
    "missing absent incomplete completeness gap gaps conflict conflicts conflicting contradiction "
    "contradictory discrepancy discrepancies disagree reconcile reconciled "
    "reconciliation correction".split()
)
SENSITIVE_REQUEST = frozenset(
    "private confidential restricted internal secret secrets watchlist reveal disclose "
    "leak dump".split()
)
TOPIC_GLUE = frozenset(
    "show display tell give get reveal disclose leak dump print please me my us our this that "
    "these those all any about can could would should do does has have had must may will "
    "private confidential restricted internal secret secrets borrower borrowers "
    "details information".split()
)


@dataclass(frozen=True)
class QueryIntent:
    """Topic requirements constrain relevance only; authorization remains a separate boundary."""

    financial_review: bool
    requires_topic_support: bool = False
    topic_terms: frozenset[str] = frozenset()


def question_words(question: str) -> frozenset[str]:
    """Keep the borrower subject without its identifier and normalize a common compound."""
    text = re.sub(r"\bborrower[-_][a-z0-9_-]+\b", " borrower ", question.lower())
    text = re.sub(r"\bwatch[ -]+list\b", "watchlist", text)
    return frozenset(terms(text))


def classify_intent(question: str) -> QueryIntent:
    """Route financial subjects and evidence-readiness requests using question text alone."""
    words = question_words(question)
    source_request = bool(
        words & {"source", "document", "documents", "evidence", "package"}
    ) and bool(
        words
        & {"request", "requested", "collect", "collected", "available", "contain", "documented"}
    )
    borrower_threshold = "threshold" in words and bool(
        words & {"borrower", "below", "missed", "meet"}
    )
    policy_only = bool(words & {"policy", "policies", "retention"}) and not bool(
        words & FINANCIAL_SUBJECTS or "borrower" in words
    )
    financial = (
        bool(words & (FINANCIAL_SUBJECTS | EVIDENCE_REVIEW)) or source_request or borrower_threshold
    ) and not policy_only
    sensitive = bool(words & SENSITIVE_REQUEST)
    return QueryIntent(financial, sensitive, words - TOPIC_GLUE if sensitive else frozenset())


def topic_supported(intent: QueryIntent, ranked: tuple[Chunk, ...]) -> bool:
    """Require requested topic words in authorized text without probing denied data."""
    if not intent.requires_topic_support:
        return True
    if not intent.topic_terms:
        return False
    return any(intent.topic_terms <= question_words(chunk.text) for chunk in ranked)
