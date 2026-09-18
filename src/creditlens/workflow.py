"""Compose authorized evidence, deterministic calculations and separately cited LLM synthesis."""

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from functools import partial
from hashlib import sha256
from time import perf_counter
from typing import TYPE_CHECKING
from uuid import uuid4

from creditlens.citations import quote, validate_citation, validate_extract
from creditlens.domain import Chunk, Packet, Principal, QueryRequest, Stage
from creditlens.errors import ServiceError
from creditlens.finance import FinanceResult, calculate_review
from creditlens.generation_contract import AnswerGenerator, validate_synthesis
from creditlens.intent import classify_intent, textual_support, topic_supported
from creditlens.response_cache import ResponseCache
from creditlens.retrieval import CanonicalCatalog, lexical_rank
from creditlens.search_provider import CachedResult, CanonicalProvider, SearchResult
from creditlens.storage import GrantStore

if TYPE_CHECKING:
    from creditlens.observability import Telemetry

ACTIONS = {
    "MEETS_POLICY": (
        "Review all remaining policy requirements; this disposition covers DSCR only.",
    ),
    "EXCEPTION_REQUIRED": ("Request a credit officer exception review with supporting rationale.",),
    "INSUFFICIENT_EVIDENCE": ("Request the missing source evidence before completing the review.",),
    "MATERIAL_CONFLICT": (
        "Reconcile conflicting source values with the borrower before continuing.",
    ),
    "HUMAN_JUDGMENT_REQUIRED": ("Review the cited evidence and make the final credit judgment.",),
}


class Trace:
    """Keep per-request stage timing separate from raw questions and evidence text."""

    def __init__(self, telemetry: "Telemetry | None" = None) -> None:
        """A request owns its timing list so concurrent queries cannot mix execution paths."""
        self.stages: list[Stage] = []
        self.telemetry = telemetry

    @contextmanager
    def span(self, name: str) -> Iterator[None]:
        """Record failed stages too, preserving diagnostic timing when a dependency raises."""
        start = perf_counter()
        try:
            with self.telemetry.span(name) if self.telemetry else nullcontext():
                yield
        finally:
            self.stages.append(Stage(name=name, duration_ms=(perf_counter() - start) * 1000))


def collect_context(
    ranked: tuple[Chunk, ...], candidates: tuple[Chunk, ...], finance: bool
) -> tuple[Chunk, ...]:
    """Explicit financial lookups are distinct from question ranking in evaluation reports."""
    required = tuple(
        chunk
        for chunk in candidates
        if finance
        and chunk.section
        in {
            "financial_summary",
            "analyst_memo",
            "application",
            "dscr.threshold",
            "dscr.exception",
            "dscr.treatment",
        }
    )
    if len(required) > 16:
        raise ServiceError("context_limit", "Required evidence exceeds the review context", 422)
    unique = {chunk.chunk_id: chunk for chunk in required + ranked}
    selected = tuple(unique.values())[:16]
    if sum(len(chunk.text.encode("utf-8")) for chunk in selected) > 16000:
        raise ServiceError("context_limit", "Evidence exceeds the supported review context", 422)
    return selected


class QueryWorkflow:
    """Preserve deterministic authority while constraining interpretation to admitted evidence."""

    def __init__(
        self,
        catalog: CanonicalCatalog,
        store: GrantStore,
        provider: CanonicalProvider | None = None,
        *,
        response_cache: ResponseCache | None = None,
        telemetry: "Telemetry | None" = None,
        generator: AnswerGenerator | None = None,
    ) -> None:
        """Inject authoritative evidence and grants for failure and revocation testing."""
        self.catalog = catalog
        self.store = store
        if provider is not None and provider.catalog is not catalog:
            raise ValueError("Workflow and provider must share the canonical catalog")
        self.provider = provider
        self.response_cache = response_cache
        self.telemetry = telemetry
        self.generator = generator

    def query(self, query: QueryRequest, principal: Principal) -> Packet:
        """Authorize, retrieve, calculate, validate, recheck grants, then acknowledge audit."""
        started = perf_counter()
        trace = Trace(self.telemetry)
        with trace.span("auth.resolve_current_grant"):
            current = self.store.resolve(principal.subject)
            if current != principal:
                raise ServiceError("access_changed", "Access changed; retry the request", 409)
        with trace.span("authorization.filter_before_retrieval"):
            candidates, revision = self.catalog.snapshot(
                current, query.borrower_id, query.effective_at
            )
        key = ResponseCache.key(query, current, revision)
        if self.generator is not None:
            key = sha256(f"{key}:{self.generator.revision}".encode()).hexdigest()
        cached = self.response_cache.get(key) if self.response_cache else None
        allowed = {c.chunk_id: c for c in candidates}
        hit = cached is not None and all(allowed.get(c.chunk_id) == c for c in cached.evidence)
        if hit and cached is not None:
            with trace.span("cache.response.hit"):
                packet = cached.model_copy(
                    update={
                        "request_id": str(uuid4()),
                        "cache_hit": True,
                        "stages": (),
                        "latency_ms": 0,
                    }
                )
            search = SearchResult((), current, query, revision, "response-cache")
        else:
            packet, search = self._generate(query, current, candidates, revision, trace)
        with trace.span("citation.validate_exact_extracts"):
            validate_packet(packet)
        with trace.span("authorization.recheck"):
            if self.provider is not None and not hit:
                if self.provider.catalog is not self.catalog:
                    raise ServiceError("access_changed", "Access changed; retry the request", 409)
                self.provider.verify(search)
            self.catalog.verify_revision(revision)
            if self.store.resolve(principal.subject) != current:
                raise ServiceError("access_changed", "Access changed; retry the request", 409)
        with trace.span("audit.persist"):
            self.store.record(
                packet.request_id,
                current,
                {
                    "borrower_id": query.borrower_id,
                    "effective_at": query.effective_at.isoformat(),
                    "chunk_ids": [c.chunk_id for c in packet.evidence],
                    "catalog_revision": revision,
                    "grant_revision": current.revision,
                    "disposition": packet.policy_disposition,
                    "stages": [s.name for s in trace.stages],
                    "provider_mode": packet.provider_mode,
                    "search_provider_mode": search.provider_mode,
                    "corpus_version": packet.corpus_version,
                    "query_hash": sha256(query.model_dump_json().encode()).hexdigest(),
                    "packet_hash": sha256(packet.model_dump_json().encode()).hexdigest(),
                    "packet_hash_scope": "packet-before-runtime-timings-v1",
                    "protected_packet": packet.model_dump(mode="json"),
                    "protected_query": query.model_dump(mode="json"),
                },
            )
        if self.response_cache and not hit:
            self.response_cache.put(key, packet)
        return packet.model_copy(
            update={"stages": tuple(trace.stages), "latency_ms": (perf_counter() - started) * 1000}
        )

    def _generate(
        self,
        query: QueryRequest,
        current: Principal,
        candidates: tuple[Chunk, ...],
        revision: int,
        trace: Trace,
    ) -> tuple[Packet, SearchResult]:
        """Reuse the original evidence and calculation path on every response-cache miss."""
        with trace.span("retrieval.local_bm25" if self.provider is None else "retrieval.provider"):
            search = self._search(query, current, candidates, revision)
            ranked = search.chunks
        if isinstance(search, CachedResult):
            with trace.span(f"cache.retrieval.{search.cache_state}"):
                pass
        with trace.span("intent.classify_question"):
            intent = classify_intent(query.question)
            finance = intent.financial_review
        with trace.span("context.check_requested_topic"):
            supported = textual_support(query.question, ranked) and topic_supported(intent, ranked)
        with trace.span("retrieval.financial_metadata_lookup" if finance else "context.build"):
            evidence = collect_context(ranked, candidates, finance) if supported else ()
        with trace.span("finance.deterministic" if finance else "answer.extractive"):
            result = (
                calculate_review(evidence) if finance else FinanceResult("HUMAN_JUDGMENT_REQUIRED")
            )
            if not supported:
                result = replace(
                    result, disposition="INSUFFICIENT_EVIDENCE", missing=("relevant evidence",)
                )
        packet = self._packet(query, result, evidence)
        if self.generator is not None:
            packet = self._synthesize(query, current, revision, packet, trace)
        if self.provider is not None:
            packet = packet.model_copy(
                update={
                    "cache_hit": isinstance(search, CachedResult) and search.cache_state == "hit",
                }
            )
        return packet, search

    def _verify_generation_authority(self, current: Principal, revision: int) -> None:
        """Recheck immediately before protected model exposure and after inference completes."""
        self.catalog.verify_revision(revision)
        if self.store.resolve(current.subject) != current:
            raise ServiceError("access_changed", "Access changed; retry the request", 409)

    def _synthesize(
        self, query: QueryRequest, current: Principal, revision: int, packet: Packet, trace: Trace
    ) -> Packet:
        """The model can add an interpretation, but cannot change financial or review authority."""
        if packet.abstained:
            return packet.model_copy(update={"provider_mode": "rag-withheld"})
        validate_packet(packet)
        if self.generator is None:
            raise ServiceError("generation_unavailable", "Answer generation is unavailable")
        with trace.span("generation.synthesize"):
            synthesis = self.generator.synthesize(
                query, packet, partial(self._verify_generation_authority, current, revision)
            )
            generated = packet.model_copy(
                update={"synthesis": synthesis, "provider_mode": "ollama-rag"}
            )
            validate_synthesis(generated)
        if self.telemetry is not None:
            self.telemetry.generation(synthesis)
        return generated

    def _search(
        self,
        query: QueryRequest,
        principal: Principal,
        candidates: tuple[Chunk, ...],
        revision: int,
    ) -> SearchResult:
        """Keep the measured local control and validate injected rankings against current scope."""
        if self.provider is None:
            return SearchResult(
                lexical_rank(query.question, candidates), principal, query, revision, "local-bm25"
            )
        if self.provider.catalog is not self.catalog:
            raise ServiceError("access_changed", "Access changed; retry the request", 409)
        result = self.provider.search(query, principal)
        self.provider.verify(result)
        allowed = {chunk.chunk_id: chunk for chunk in candidates}
        if (
            result.principal != principal
            or result.request != query
            or result.catalog_revision != revision
            or len(result.chunks) > 10
            or len({chunk.chunk_id for chunk in result.chunks}) != len(result.chunks)
            or any(allowed.get(chunk.chunk_id) != chunk for chunk in result.chunks)
        ):
            raise ServiceError("invalid_search_result", "Search result is unavailable", 503)
        self.catalog.verify_revision(revision)
        return result

    def _packet(
        self, query: QueryRequest, result: FinanceResult, evidence: tuple[Chunk, ...]
    ) -> Packet:
        """Use quoted facts and fixed actions while limiting the disposition to DSCR."""
        return Packet(
            request_id=str(uuid4()),
            borrower_id=query.borrower_id,
            borrower_summary=tuple(quote(c) for c in evidence if c.borrower_id is not None),
            applicable_policy=tuple(quote(c) for c in evidence if c.borrower_id is None),
            calculated_metrics=result.metrics,
            policy_disposition=result.disposition,
            missing_documents=result.missing,
            contradictions=result.conflicts,
            exceptions=tuple(
                quote(c)
                for c in evidence
                if c.section == "dscr.exception" and result.disposition == "EXCEPTION_REQUIRED"
            ),
            recommended_next_actions=ACTIONS[result.disposition],
            questions_for_underwriter=(
                "Have the source documents and all applicable policy requirements been reviewed?",
            ),
            abstained=result.disposition in ("INSUFFICIENT_EVIDENCE", "MATERIAL_CONFLICT"),
            evidence=evidence,
            provider_mode="local-extractive",
            corpus_version=self.catalog.version,
            latency_ms=0,
        )


def validate_packet(packet: Packet) -> None:
    """Apply exact-support checks to every factual field and provenance to every metric input."""
    claims = (
        packet.borrower_summary
        + packet.applicable_policy
        + packet.exceptions
        + packet.contradictions
    )
    for claim in claims:
        validate_extract(claim, packet.evidence)
    for metric in packet.calculated_metrics:
        for citation in metric.citations:
            validate_citation(citation, packet.evidence)
    if packet.recommended_next_actions != ACTIONS[packet.policy_disposition]:
        raise ServiceError("unsupported_action", "Review actions could not be validated")
    validate_synthesis(packet)
