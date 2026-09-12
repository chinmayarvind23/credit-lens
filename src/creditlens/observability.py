"""Opt-in local OTel traces and bounded Prometheus labels, without remote telemetry calls."""

import json
import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import perf_counter
from typing import Any

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from starlette.datastructures import State
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from creditlens.domain import Packet

ROUTES = frozenset(
    {
        "/health",
        "/ready",
        "/api/v1/query",
        "/api/v1/underwriting-packet",
        "/api/v1/borrowers",
        "/api/v1/metrics",
    }
)
STAGES = frozenset(
    {
        "auth.resolve_current_grant",
        "authorization.filter_before_retrieval",
        "cache.response.hit",
        "citation.validate_exact_extracts",
        "authorization.recheck",
        "audit.persist",
        "retrieval.local_bm25",
        "retrieval.provider",
        "cache.retrieval.hit",
        "cache.retrieval.miss",
        "cache.retrieval.disabled",
        "intent.classify_question",
        "context.check_requested_topic",
        "retrieval.financial_metadata_lookup",
        "context.build",
        "finance.deterministic",
        "answer.extractive",
    }
)


class LocalSpanExporter(SpanExporter):
    """Export allowed attributes only, excluding exception events and resource discovery."""

    def __init__(self, path: Path) -> None:
        """Rotate finite local files instead of sending data to a hosted collector."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handler = RotatingFileHandler(
            path, maxBytes=5_000_000, backupCount=2, encoding="utf-8"
        )
        self.handler.setFormatter(logging.Formatter("%(message)s"))

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """Retain correlation and timings while excluding identities, inputs and payloads."""
        for span in spans:
            context = span.context
            if context is None:
                continue
            value = {
                "name": span.name,
                "trace_id": f"{context.trace_id:032x}",
                "span_id": f"{context.span_id:016x}",
                "parent_span_id": f"{span.parent.span_id:016x}" if span.parent else None,
                "start_time": span.start_time,
                "end_time": span.end_time,
                "status": span.status.status_code.name,
                "attributes": {
                    k: v
                    for k, v in (span.attributes or {}).items()
                    if k in {"http.route", "http.status_code", "http.request.method"}
                },
            }
            self.handler.handle(
                logging.LogRecord(
                    "creditlens.trace", logging.INFO, "", 0, json.dumps(value), (), None
                )
            )
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        """Close the owned file descriptor during application shutdown."""
        self.handler.close()


class Telemetry:
    """Each app owns an SDK provider and registry; tests and concurrent apps cannot share labels."""

    def __init__(self, trace_file: str = "") -> None:
        """The provider has no network exporters or default host/process resource detectors."""
        self.provider = TracerProvider(resource=Resource({"service.name": "creditlens"}))
        if trace_file:
            self.provider.add_span_processor(
                SimpleSpanProcessor(LocalSpanExporter(Path(trace_file)))
            )
        self.tracer = self.provider.get_tracer("creditlens.workflow", "1")
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "creditlens_requests_total",
            "Completed HTTP requests",
            ["route", "method", "status"],
            registry=self.registry,
        )
        self.duration = Histogram(
            "creditlens_request_duration_seconds",
            "Complete HTTP duration",
            ["route"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.7, 5, 10, 30),
            registry=self.registry,
        )
        self.packets = Counter(
            "creditlens_packets_total",
            "Audited evidence packets",
            ["disposition", "cache"],
            registry=self.registry,
        )
        self.stage_duration = Histogram(
            "creditlens_stage_duration_seconds",
            "Workflow stage duration, including failures",
            ["stage"],
            registry=self.registry,
        )
        self.stage_errors = Counter(
            "creditlens_stage_errors_total",
            "Failed workflow stages",
            ["stage"],
            registry=self.registry,
        )
        self.abstentions = Counter(
            "creditlens_abstentions_total",
            "Audited packets that abstain",
            registry=self.registry,
        )
        self.denials = Counter(
            "creditlens_acl_denials_total",
            "HTTP authentication or authorization denials",
            ["status"],
            registry=self.registry,
        )

    @contextmanager
    def span(self, name: str) -> Iterator[None]:
        """Disable SDK exception capture because exception messages can contain private payloads."""
        label = name if name in STAGES else "other"
        started = perf_counter()
        with self.tracer.start_as_current_span(
            label, record_exception=False, set_status_on_exception=False
        ) as span:
            try:
                yield
            except Exception:
                self.stage_errors.labels(label).inc()
                span.set_status(StatusCode.ERROR)
                raise
            finally:
                self.stage_duration.labels(label).observe(perf_counter() - started)

    def packet(self, packet: Packet) -> None:
        """Expose only finite disposition/cache labels after the protected audit has succeeded."""
        self.packets.labels(packet.policy_disposition, "hit" if packet.cache_hit else "miss").inc()
        if packet.abstained:
            self.abstentions.inc()

    def render(self) -> bytes:
        """Generate standard Prometheus exposition without private identifiers or source text."""
        return generate_latest(self.registry)

    def close(self) -> None:
        """Drain processors and close local exporters; no global provider state was modified."""
        self.provider.shutdown()


class TelemetryMiddleware:
    """Observe failures as well as successful handlers with finite route labels and an OTel root."""

    def __init__(self, app: ASGIApp, state: State) -> None:
        """Resolve the app-owned telemetry after lifespan initializes it."""
        self.app, self.state = app, state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Trace request processing without reading headers, query strings or body contents."""
        telemetry: Telemetry | None = self.state.telemetry
        if scope["type"] != "http" or telemetry is None:
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        route = (
            path
            if path in ROUTES
            else "/api/v1/evidence/{id}"
            if path.startswith("/api/v1/evidence/")
            else "/other"
        )
        method = scope["method"] if scope["method"] in {"GET", "POST", "OPTIONS"} else "OTHER"
        status = 500
        started = perf_counter()

        async def observe(message: Message) -> None:
            """Record only the response status, leaving all payloads untouched."""
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        with telemetry.tracer.start_as_current_span(
            "http.request", record_exception=False, set_status_on_exception=False
        ) as span:
            try:
                await self.app(scope, receive, observe)
            finally:
                attributes: dict[str, Any] = {
                    "http.route": route,
                    "http.request.method": method,
                    "http.status_code": status,
                }
                span.set_attributes(attributes)
                if status >= 500:
                    span.set_status(StatusCode.ERROR)
                telemetry.requests.labels(route, method, f"{status // 100}xx").inc()
                telemetry.duration.labels(route).observe(perf_counter() - started)
                if status in {401, 403}:
                    telemetry.denials.labels(str(status)).inc()
