"""Shared contracts bind facts to immutable, authorized page evidence."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """Reject caller-injected fields so scope cannot hide inside loosely typed requests."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Principal(StrictModel):
    """Only a trusted grant resolver constructs the effective request identity."""

    subject: str
    tenant_id: str
    role: Literal["underwriter", "admin", "reviewer"]
    borrower_ids: tuple[str, ...]
    acl_groups: tuple[str, ...]
    revision: int = Field(ge=1)


class Page(StrictModel):
    """One physical page is the invariant citation unit across chunking experiments."""

    tenant_id: str
    borrower_id: str | None
    document_id: str
    document_version: str
    page: int = Field(ge=1)
    document_kind: str
    title: str
    section: str
    text: str = Field(min_length=1)
    acl_groups: tuple[str, ...] = Field(min_length=1)
    valid_from: date
    valid_to: date | None = None
    content_hash: str
    parser_version: str
    extraction_confidence: float = Field(default=1, ge=0, le=1)

    @model_validator(mode="after")
    def ordered_dates(self) -> "Page":
        """Half-open date windows avoid accidentally active superseded policy versions."""
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must follow valid_from")
        return self


class Chunk(Page):
    """Chunks stay within a page and retain offsets for exact support validation."""

    chunk_id: str
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    chunker_version: str

    @model_validator(mode="after")
    def ordered_offsets(self) -> "Chunk":
        """Reject empty or reversed source spans before a citation can refer to them."""
        if self.end_char <= self.start_char:
            raise ValueError("end_char must follow start_char")
        return self


class Citation(StrictModel):
    """Provenance identifies a retrieved chunk, never a model-invented source URL."""

    document_id: str
    document_version: str
    page: int = Field(ge=1)
    chunk_id: str


class Claim(StrictModel):
    """Every factual field uses this contract, including the borrower summary."""

    text: str = Field(min_length=1)
    citations: tuple[Citation, ...] = Field(min_length=1)


class FinancialMetric(StrictModel):
    """Decimal arithmetic is accompanied by the provenance of its source inputs."""

    name: str
    value: Decimal
    unit: str
    source_fields: tuple[str, ...]
    citations: tuple[Citation, ...] = Field(min_length=1)


class QueryRequest(StrictModel):
    """The public request deliberately has no tenant, role, ACL or ranking controls."""

    borrower_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    question: str = Field(min_length=3, max_length=2000)
    effective_at: date = Field(default_factory=date.today)


class Borrower(StrictModel):
    """Only authorized borrower summaries are returned to the browser selector."""

    borrower_id: str
    name: str
    industry: str


class Stage(StrictModel):
    """Trace payloads expose execution order and timing without raw prompt text."""

    name: str
    duration_ms: float = Field(ge=0)


class Packet(StrictModel):
    """A preparation artifact has evidence and review states, never a loan decision."""

    request_id: str
    borrower_id: str
    borrower_summary: tuple[Claim, ...] = ()
    calculated_metrics: tuple[FinancialMetric, ...] = ()
    applicable_policy: tuple[Claim, ...] = ()
    policy_disposition: Literal[
        "MEETS_POLICY",
        "EXCEPTION_REQUIRED",
        "INSUFFICIENT_EVIDENCE",
        "MATERIAL_CONFLICT",
        "HUMAN_JUDGMENT_REQUIRED",
    ]
    missing_documents: tuple[str, ...] = ()
    exceptions: tuple[Claim, ...] = ()
    contradictions: tuple[Claim, ...] = ()
    recommended_next_actions: tuple[str, ...] = ()
    questions_for_underwriter: tuple[str, ...] = ()
    abstained: bool
    evidence: tuple[Chunk, ...] = ()
    stages: tuple[Stage, ...] = ()
    provider_mode: str
    corpus_version: str
    latency_ms: float = Field(ge=0)
    cache_hit: bool = False
    cost_usd: Decimal | None = None
