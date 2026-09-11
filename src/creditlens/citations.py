"""Exact extraction support is distinct from independent semantic claim grading."""

from creditlens.domain import Chunk, Citation, Claim
from creditlens.errors import ServiceError


def cite(chunk: Chunk) -> Citation:
    """Copy source identity from trusted retrieved evidence, never from generated text."""
    return Citation(
        document_id=chunk.document_id,
        document_version=chunk.document_version,
        page=chunk.page,
        chunk_id=chunk.chunk_id,
    )


def quote(chunk: Chunk) -> Claim:
    """The offline demonstration serves exact evidence excerpts with explicit provenance."""
    return Claim(text=chunk.text.strip(), citations=(cite(chunk),))


def validate_citation(citation: Citation, evidence: tuple[Chunk, ...]) -> Chunk:
    """Only the exact retrieved document-version-page tuple is eligible as a citation."""
    chunks = {chunk.chunk_id: chunk for chunk in evidence}
    chunk = chunks.get(citation.chunk_id)
    if chunk is None or cite(chunk) != citation:
        raise ServiceError("invalid_citation", "The response could not be supported by evidence")
    return chunk


def validate_extract(claim: Claim, evidence: tuple[Chunk, ...]) -> None:
    """Reject fabricated text even when its citation identifiers are structurally valid."""
    for citation in claim.citations:
        chunk = validate_citation(citation, evidence)
        if claim.text not in chunk.text:
            raise ServiceError(
                "unsupported_claim", "The response could not be supported by evidence"
            )
