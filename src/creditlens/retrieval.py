"""Page-bounded lexical control with authorization before any relevance scoring."""

import json
import math
import re
from collections import Counter
from datetime import date
from hashlib import sha256
from threading import RLock
from typing import Literal, Protocol

from creditlens.auth import authorize_borrower, authorized_page
from creditlens.domain import Chunk, Page, Principal
from creditlens.errors import ServiceError

STOP_WORDS = frozenset(
    "a an and are as at be by for from how in is it of on or the to what which with".split()
)


def terms(text: str) -> list[str]:
    """Keep exact financial identifiers and numbers while removing low-information glue words."""
    return [word for word in re.findall(r"[a-z0-9_]+", text.lower()) if word not in STOP_WORDS]


def chunk_page(
    page: Page, size: int = 1200, strategy: Literal["fixed", "paragraph"] = "paragraph"
) -> tuple[Chunk, ...]:
    """Stable offsets never cross pages; paragraph boundaries preserve financial field groups."""
    if size < 200 or size > 8000:
        raise ValueError("Chunk size must be between 200 and 8000 characters")
    chunks = []
    start = 0
    while start < len(page.text):
        end = min(start + size, len(page.text))
        if strategy == "paragraph" and end < len(page.text):
            boundary = page.text.rfind("\n", start + size // 2, end)
            if boundary > start:
                end = boundary + 1
        identity = json.dumps(
            [
                page.tenant_id,
                page.document_id,
                page.document_version,
                page.page,
                page.content_hash,
                start,
                end,
                strategy,
                size,
            ]
        )
        chunks.append(
            Chunk.model_validate(
                page.model_dump()
                | {
                    "text": page.text[start:end],
                    "chunk_id": sha256(identity.encode()).hexdigest(),
                    "start_char": start,
                    "end_char": end,
                    "chunker_version": f"{strategy}-v1-{size}",
                }
            )
        )
        start = end
    return tuple(chunks)


class CanonicalCatalog(Protocol):
    """Memory and PostgreSQL supply the same authoritative, scoped snapshot contract."""

    @property
    def version(self) -> str:
        """Identify canonical content and authority for cache and audit provenance."""
        ...

    def snapshot(
        self, principal: Principal, borrower_id: str, effective_at: date
    ) -> tuple[tuple[Chunk, ...], int]:
        """Filter authorization before returning candidates and their consistent epoch."""
        ...

    def verify_revision(self, revision: int) -> None:
        """Reject an obsolete snapshot against the currently committed authority."""
        ...


class EvidenceCatalog:
    """A local immutable snapshot gives demo requests an explicit revocation epoch."""

    def __init__(self, pages: tuple[Page, ...]) -> None:
        """Reject duplicate page identities so conflicting versions cannot overwrite provenance."""
        keys = [(p.tenant_id, p.document_id, p.document_version, p.page) for p in pages]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate canonical page identity")
        self._chunks = tuple(chunk for page in pages for chunk in chunk_page(page))
        self._revoked: set[str] = set()
        self._revision = 1
        self._lock = RLock()
        self.version = sha256("".join(c.chunk_id for c in self._chunks).encode()).hexdigest()

    def snapshot(
        self, principal: Principal, borrower_id: str, effective_at: date
    ) -> tuple[tuple[Chunk, ...], int]:
        """Compute an authorized candidate set before lexical scores or a provider request exist."""
        authorize_borrower(principal, borrower_id)
        with self._lock:
            candidates = tuple(
                c
                for c in self._chunks
                if c.chunk_id not in self._revoked
                and authorized_page(c, principal, borrower_id, effective_at)
            )
            return candidates, self._revision

    def revoke(self, chunk_id: str) -> None:
        """Revoke the referenced page and every sibling chunk, then invalidate snapshots."""
        with self._lock:
            source = next((chunk for chunk in self._chunks if chunk.chunk_id == chunk_id), None)
            if source is None:
                raise ValueError("Unknown evidence chunk")
            self._revoked.update(
                chunk.chunk_id
                for chunk in self._chunks
                if (chunk.tenant_id, chunk.document_id, chunk.document_version, chunk.page)
                == (source.tenant_id, source.document_id, source.document_version, source.page)
            )
            self._revision += 1

    def verify_revision(self, revision: int) -> None:
        """Do not serve a result assembled under a now-stale authorization snapshot."""
        with self._lock:
            if revision != self._revision:
                raise ServiceError(
                    "evidence_changed", "Evidence permissions changed; retry the request", 409
                )


def lexical_rank(
    question: str, candidates: tuple[Chunk, ...], limit: int = 10
) -> tuple[Chunk, ...]:
    """BM25 is a transparent local control; it is not labeled as Cortex or dense retrieval."""
    if not 1 <= limit <= 100:
        raise ValueError("Retrieval limit must be between 1 and 100")
    if not candidates:
        return ()
    query = set(terms(question))
    counts = [Counter(terms(c.text + " " + c.section)) for c in candidates]
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / len(lengths) or 1
    frequencies = Counter(word for count in counts for word in count)
    scores = [
        bm25(query, count, length, average, frequencies, len(counts))
        for count, length in zip(counts, lengths, strict=True)
    ]
    ranking = sorted(
        zip(candidates, scores, strict=True), key=lambda pair: (-pair[1], pair[0].chunk_id)
    )
    return tuple(chunk for chunk, score in ranking[:limit] if score > 0)


def bm25(
    query: set[str],
    count: Counter[str],
    length: int,
    average: float,
    frequencies: Counter[str],
    documents: int,
) -> float:
    """Use fixed k1=1.2 and b=.75 until a frozen benchmark justifies changing them."""
    score = 0.0
    for term in query:
        frequency = count[term]
        inverse_frequency = math.log(
            1 + (documents - frequencies[term] + 0.5) / (frequencies[term] + 0.5)
        )
        score += (
            inverse_frequency
            * frequency
            * 2.2
            / (frequency + 1.2 * (0.25 + 0.75 * length / average))
        )
    return score


def reciprocal_rank_fusion(
    rankings: tuple[tuple[Chunk, ...], ...], limit: int = 10, k: int = 60
) -> tuple[Chunk, ...]:
    """Combine ranks without pretending lexical and vector score magnitudes are comparable."""
    if k <= 0 or limit <= 0:
        raise ValueError("RRF limits must be positive")
    scores: dict[str, float] = {}
    chunks: dict[str, Chunk] = {}
    for ranking in rankings:
        seen: set[str] = set()
        for rank, chunk in enumerate(ranking, start=1):
            if chunk.chunk_id not in seen:
                scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0) + 1 / (k + rank)
                chunks[chunk.chunk_id] = chunk
                seen.add(chunk.chunk_id)
    return tuple(chunks[key] for key in sorted(scores, key=lambda key: (-scores[key], key))[:limit])
