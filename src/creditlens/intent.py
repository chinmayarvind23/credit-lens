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
RELEVANCE_GLUE = TOPIC_GLUE | frozenset(
    "when where who whom whose why how what which whether explain describe discuss summarize "
    "summary prepare provide find answer question tell according relevant available".split()
)
ASSESSMENT_CUES = frozenset(
    "calculate calculated compute computed assess assessing evaluate compare compared "
    "satisfy satisfies meet meets met miss missed below above exceed exceeds shortfall "
    "result results imply issue raise cover covers need needs needed sufficient insufficient "
    "packet underwriting".split()
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
    ) and not (
        policy_only or policy_threshold_lookup(words) or policy_procedure_lookup(words, question)
    )
    sensitive = bool(words & SENSITIVE_REQUEST)
    return QueryIntent(financial, sensitive, words - TOPIC_GLUE if sensitive else frozenset())


def policy_threshold_lookup(words: frozenset[str]) -> bool:
    """Reference thresholds need policy evidence, while assessment/readiness cues retain finance."""
    return bool(words & {"minimum", "threshold"}) and not bool(
        words & (ASSESSMENT_CUES | EVIDENCE_REVIEW)
    )


def policy_procedure_lookup(words: frozenset[str], question: str) -> bool:
    """General policy procedures quote rules without assessing an unrelated borrower ratio."""
    procedural = bool(
        set(re.findall(r"[a-z]+", question.lower()))
        & {"how", "explain", "describe", "procedure", "process", "rule", "rules"}
    )
    subject = bool(words & {"policy", "policies", "exception", "exceptions", "treatment"})
    assessment = bool(words & (ASSESSMENT_CUES | {"borrower", "missing", "absent", "incomplete"}))
    return procedural and subject and not assessment


def topic_supported(intent: QueryIntent, ranked: tuple[Chunk, ...]) -> bool:
    """Require requested topic words in authorized text without probing denied data."""
    if not intent.requires_topic_support:
        return True
    if not intent.topic_terms:
        return False
    return any(intent.topic_terms <= question_words(chunk.text) for chunk in ranked)


def textual_support(question: str, ranked: tuple[Chunk, ...]) -> bool:
    """Nearest neighbors alone cannot admit a packet; require a non-generic textual topic anchor."""
    topics = {word for word in question_words(question) - RELEVANCE_GLUE if not word.isdecimal()}
    return any(topics & question_words(chunk.text) for chunk in ranked)
