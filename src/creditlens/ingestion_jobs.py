"""PostgreSQL owns ingestion intent, duplicate suppression and fenced worker leases."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    and_,
    func,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import SQLAlchemyError

from creditlens.auth import authorize_borrower
from creditlens.domain import Page, Principal, StrictModel
from creditlens.errors import ServiceError
from creditlens.ingestion import _validate_metadata, text_hash
from creditlens.ocr import OcrDocument, OcrReview
from creditlens.sql_catalog import SqlEvidenceCatalog
from creditlens.storage import grants

JobState = Literal["QUEUED", "RUNNING", "RETRY", "REVIEW_REQUIRED", "COMPLETED", "FAILED"]
FailureCode = Literal[
    "source_unavailable",
    "invalid_source",
    "extraction_failed",
    "permission_changed",
    "worker_timeout",
    "worker_unavailable",
    "attempts_exhausted",
    "publication_failed",
    "review_rejected",
]
FAILURES = {
    "source_unavailable",
    "invalid_source",
    "extraction_failed",
    "permission_changed",
    "worker_timeout",
    "worker_unavailable",
    "attempts_exhausted",
    "publication_failed",
    "review_rejected",
}
schema = MetaData()
jobs = Table(
    "ingestion_jobs",
    schema,
    Column("job_id", String, primary_key=True),
    Column("queue_id", String, nullable=False, index=True),
    Column("tenant_id", String, nullable=False),
    Column("subject", String, nullable=False),
    Column("idempotency_key", String, nullable=False),
    Column("fingerprint", String, nullable=False),
    Column("input", JSON, nullable=False),
    Column("state", String, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("lease_until", DateTime(timezone=True)),
    Column("lease_token", String),
    Column("error_code", String),
    Column("result", JSON),
    UniqueConstraint("queue_id", "tenant_id", "subject", "idempotency_key"),
)


class IngestionInput(StrictModel):
    """An immutable content hash names a staged PDF; metadata never supplies extracted text."""

    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    pages: tuple[Page, ...] = Field(min_length=1, max_length=1000)
    parser: Literal["digital", "ocr"]

    @model_validator(mode="after")
    def valid_manifest(self) -> "IngestionInput":
        """Keep physical page order and a single document scope across all worker attempts."""
        _validate_metadata(self.pages)
        if len(self.model_dump_json().encode()) > 1_000_000:
            raise ValueError("Ingestion manifest exceeds size limit")
        return self

    def sanitized(self) -> "IngestionInput":
        """Discard prefilled text so idempotency and workers depend only on real source bytes."""
        pending = tuple(
            page.model_copy(
                update={
                    "text": "pending extraction",
                    "content_hash": text_hash("pending extraction"),
                    "parser_version": "pending",
                    "extraction_confidence": 0,
                }
            )
            for page in self.pages
        )
        return IngestionInput(source_sha256=self.source_sha256, pages=pending, parser=self.parser)


class JobStatus(StrictModel):
    """Public status omits source content, worker fencing tokens and raw exception messages."""

    job_id: str
    state: JobState
    attempts: int
    error_code: FailureCode | None
    created_at: datetime
    updated_at: datetime


class JobLease(StrictModel):
    """Only the trusted worker receives this attempt's token and immutable extraction manifest."""

    job_id: str
    queue_id: str
    token: str = Field(repr=False)
    subject: str
    attempt: int
    input: IngestionInput


def initialize_jobs(engine: Engine) -> None:
    """Make schema creation explicit; SQLite cannot establish the required row-lock semantics."""
    if engine.dialect.name != "postgresql":
        raise ValueError("Durable ingestion requires PostgreSQL")
    schema.create_all(engine)


def _authorize(principal: Principal, source: IngestionInput) -> None:
    """An admin still needs current tenant, borrower and every page ACL in its effective grant."""
    if principal.role != "admin":
        raise ServiceError("access_denied", "Ingestion is not authorized", 403)
    for page in source.pages:
        if page.tenant_id != principal.tenant_id or not set(page.acl_groups).issubset(
            principal.acl_groups
        ):
            raise ServiceError("access_denied", "Ingestion is not authorized", 403)
        if page.borrower_id is not None:
            authorize_borrower(principal, page.borrower_id)


def _status(row: RowMapping) -> JobStatus:
    """Use an allowlist so adding private SQL columns never expands the public response."""
    return JobStatus.model_validate({key: row[key] for key in JobStatus.model_fields})


def _current_publisher(connection: Connection, subject: str, source: IngestionInput) -> Principal:
    """Lock the current grant through commit so concurrent revocation cannot race admission."""
    row = (
        connection.execute(
            select(grants).where(grants.c.subject == subject).with_for_update(read=True)
        )
        .mappings()
        .first()
    )
    if row is None or not row["enabled"]:
        raise ServiceError("permission_changed", "Ingestion permission is no longer current", 403)
    principal = Principal.model_validate({key: row[key] for key in Principal.model_fields})
    _authorize(principal, source)
    return principal


def _validate_extracted(source: IngestionInput, extracted: tuple[Page, ...]) -> None:
    """Extraction may change text and parser fields, never the trusted source manifest's scope."""
    if len(extracted) != len(source.pages):
        raise ValueError("Extracted page count differs from the physical manifest")
    mutable = {"text", "content_hash", "parser_version", "extraction_confidence"}
    for original, page in zip(source.pages, extracted, strict=True):
        page = Page.model_validate(page.model_dump())
        if original.model_dump(exclude=mutable) != page.model_dump(exclude=mutable):
            raise ValueError("Extracted page metadata differs from the trusted manifest")
        if page.extraction_confidence < 0.9 or page.parser_version == "pending":
            raise ValueError("Extracted evidence has not passed the admission threshold")


class JobStore:
    """Coordinate workers with database time and random fencing tokens, not queue delivery count."""

    def __init__(self, engine: Engine, queue_id: str, *, lease_seconds: int = 600) -> None:
        """Bind workers to an explicit queue and bounded lease while reusing the database pool."""
        if engine.dialect.name != "postgresql" or not re.fullmatch(
            r"[a-zA-Z0-9_-]{1,80}", queue_id
        ):
            raise ValueError("A named PostgreSQL ingestion queue is required")
        if not 10 <= lease_seconds <= 900:
            raise ValueError("Ingestion lease must be 10..900 seconds")
        self.engine = engine.execution_options(isolation_level="READ COMMITTED")
        self.queue_id, self.lease_seconds = queue_id, lease_seconds

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        """Bound lock and statement waits, roll back failures and expose only curated errors."""
        try:
            with self.engine.begin() as connection:
                connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
                connection.exec_driver_sql("SET LOCAL lock_timeout = '2s'")
                yield connection
        except SQLAlchemyError as error:
            raise ServiceError(
                "ingestion_unavailable", "Ingestion storage is unavailable", 503
            ) from error

    def submit(self, source: IngestionInput, principal: Principal, key: str) -> JobStatus:
        """Atomically suppress duplicate intent while rejecting changed input under the same key."""
        source = IngestionInput.model_validate(source.model_dump()).sanitized()
        _authorize(principal, source)
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", key):
            raise ValueError("Invalid ingestion idempotency key")
        fingerprint = sha256(source.model_dump_json().encode()).hexdigest()
        predicate = and_(
            jobs.c.queue_id == self.queue_id,
            jobs.c.tenant_id == principal.tenant_id,
            jobs.c.subject == principal.subject,
            jobs.c.idempotency_key == key,
        )
        with self._transaction() as connection:
            connection.execute(
                insert(jobs)
                .values(
                    job_id=str(uuid4()),
                    queue_id=self.queue_id,
                    tenant_id=principal.tenant_id,
                    subject=principal.subject,
                    idempotency_key=key,
                    fingerprint=fingerprint,
                    input=source.model_dump(mode="json"),
                    state="QUEUED",
                    attempts=0,
                    created_at=func.clock_timestamp(),
                    updated_at=func.clock_timestamp(),
                    available_at=func.clock_timestamp(),
                )
                .on_conflict_do_nothing()
            )
            row = connection.execute(select(jobs).where(predicate)).mappings().one()
            if row["fingerprint"] != fingerprint:
                raise ServiceError(
                    "idempotency_conflict", "Idempotency key has different input", 409
                )
            return _status(row)

    def status(self, job_id: str, principal: Principal) -> JobStatus:
        """Filter tenant before hydration and recheck current borrower/ACL scope on every read."""
        if principal.role != "admin":
            raise ServiceError("access_denied", "Ingestion is not authorized", 403)
        with self._transaction() as connection:
            row = (
                connection.execute(
                    select(jobs).where(
                        jobs.c.queue_id == self.queue_id,
                        jobs.c.job_id == job_id,
                        jobs.c.tenant_id == principal.tenant_id,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ServiceError("job_not_found", "Ingestion job is unavailable", 404)
            _authorize(principal, IngestionInput.model_validate(row["input"]))
            return _status(row)

    def check_ready(self) -> None:
        """Verify the configured job table is readable without exposing another job's content."""
        with self._transaction() as connection:
            connection.execute(
                select(jobs.c.job_id).where(jobs.c.queue_id == self.queue_id).limit(1)
            )

    def delivery_disposition(self, job_id: str) -> Literal["TERMINAL", "PENDING", "UNKNOWN"]:
        """Inspect only this queue; broker messages supply no permission claims."""
        with self._transaction() as connection:
            row = (
                connection.execute(
                    select(jobs.c.state, jobs.c.input["parser"].as_string().label("parser")).where(
                        jobs.c.queue_id == self.queue_id, jobs.c.job_id == job_id
                    )
                )
                .mappings()
                .first()
            )
            if row is None or row["parser"] != "digital":
                return "UNKNOWN"
            return "TERMINAL" if row["state"] in {"COMPLETED", "FAILED"} else "PENDING"

    def claim(
        self, job_id: str | None = None, *, parser: Literal["digital", "ocr"] | None = None
    ) -> JobLease | None:
        """Skip locked jobs and reclaim expired work with a fresh token and bounded attempts."""
        available = or_(
            and_(
                jobs.c.state.in_(["QUEUED", "RETRY"]), jobs.c.available_at <= func.clock_timestamp()
            ),
            and_(jobs.c.state == "RUNNING", jobs.c.lease_until <= func.clock_timestamp()),
        )
        statement = select(jobs).where(jobs.c.queue_id == self.queue_id, available)
        if parser is not None:
            statement = statement.where(jobs.c.input["parser"].as_string() == parser)
        if job_id is not None:
            statement = statement.where(jobs.c.job_id == job_id)
        with self._transaction() as connection:
            row = (
                connection.execute(
                    statement.order_by(jobs.c.created_at, jobs.c.job_id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            if row["attempts"] >= 3:
                self._change(
                    connection,
                    row["job_id"],
                    state="FAILED",
                    error_code="attempts_exhausted",
                    lease_until=None,
                    lease_token=None,
                )
                return None
            token, attempt = str(uuid4()), int(row["attempts"]) + 1
            self._change(
                connection,
                row["job_id"],
                state="RUNNING",
                attempts=attempt,
                lease_token=token,
                error_code=None,
                lease_until=func.clock_timestamp() + timedelta(seconds=self.lease_seconds),
            )
            return JobLease(
                job_id=row["job_id"],
                queue_id=self.queue_id,
                token=token,
                subject=row["subject"],
                attempt=attempt,
                input=IngestionInput.model_validate(row["input"]),
            )

    def _locked(self, connection: Connection, lease: JobLease) -> RowMapping:
        """Expired or replaced workers cannot mutate state using previously extracted results."""
        row = (
            connection.execute(
                select(jobs)
                .where(
                    jobs.c.queue_id == self.queue_id,
                    jobs.c.queue_id == lease.queue_id,
                    jobs.c.job_id == lease.job_id,
                    jobs.c.state == "RUNNING",
                    jobs.c.lease_token == lease.token,
                    jobs.c.lease_until > func.clock_timestamp(),
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if row is None or row["lease_until"] <= connection.scalar(select(func.clock_timestamp())):
            raise ServiceError("lease_lost", "Ingestion worker lease is no longer current", 409)
        return row

    def _change(self, connection: Connection, job_id: str, **values: Any) -> None:
        """All callers hold the job row lock; record database time for every visible transition."""
        connection.execute(
            jobs.update()
            .where(jobs.c.job_id == job_id, jobs.c.queue_id == self.queue_id)
            .values(updated_at=func.clock_timestamp(), **values)
        )

    def heartbeat(self, lease: JobLease) -> None:
        """Renew current ownership and grants; revoked submitters cannot keep extraction alive."""
        with self._transaction() as connection:
            row = self._locked(connection, lease)
            _current_publisher(
                connection, row["subject"], IngestionInput.model_validate(row["input"])
            )
            self._change(
                connection,
                lease.job_id,
                lease_until=func.clock_timestamp() + timedelta(seconds=self.lease_seconds),
            )

    def fail(self, lease: JobLease, code: FailureCode, *, retryable: bool) -> None:
        """Persist curated failures with delayed retries and a hard three-attempt ceiling."""
        if code not in FAILURES:
            raise ValueError("Unknown ingestion failure code")
        with self._transaction() as connection:
            row = self._locked(connection, lease)
            exhausted = int(row["attempts"]) >= 3
            self._change(
                connection,
                lease.job_id,
                state="RETRY" if retryable and not exhausted else "FAILED",
                error_code="attempts_exhausted" if retryable and exhausted else code,
                available_at=func.clock_timestamp() + timedelta(seconds=10 * row["attempts"]),
                lease_token=None,
                lease_until=None,
            )

    def require_review(self, lease: JobLease, artifact_sha256: str) -> None:
        """Quarantine a completed extraction artifact without admitting any uncertain evidence."""
        if not re.fullmatch(r"[a-f0-9]{64}", artifact_sha256):
            raise ValueError("Review requires a hashed extraction artifact")
        with self._transaction() as connection:
            self._locked(connection, lease)
            self._change(
                connection,
                lease.job_id,
                state="REVIEW_REQUIRED",
                lease_until=None,
                lease_token=None,
                result={"artifact_sha256": artifact_sha256},
            )

    def complete(
        self, lease: JobLease, catalog: SqlEvidenceCatalog, batch: tuple[Page, ...]
    ) -> None:
        """Fence, reauthorize and atomically commit canonical pages plus a completed job."""
        with self._transaction() as connection:
            row = self._locked(connection, lease)
            source = IngestionInput.model_validate(row["input"])
            if source.parser != "digital":
                raise ServiceError("review_required", "OCR evidence requires review", 409)
            _validate_extracted(source, batch)
            _current_publisher(connection, row["subject"], source)
            revision = catalog.publish_in_transaction(connection, batch)
            # Recheck after database waits/work so expiry rolls back publication too.
            self._locked(connection, lease)
            self._change(
                connection,
                lease.job_id,
                state="COMPLETED",
                lease_until=None,
                lease_token=None,
                error_code=None,
                result={
                    "catalog_id": catalog.catalog_id,
                    "revision": revision,
                    "physical_pages": len(batch),
                },
            )

    def stage_ocr(self, lease: JobLease, artifact: OcrDocument) -> None:
        """Quarantine trusted offline extraction under the current fenced OCR lease."""
        artifact = OcrDocument.model_validate(artifact.model_dump())
        with self._transaction() as connection:
            row = self._locked(connection, lease)
            source = IngestionInput.model_validate(row["input"])
            _validate_ocr(source, artifact)
            _current_publisher(connection, row["subject"], source)
            self._locked(connection, lease)
            self._change(
                connection,
                lease.job_id,
                state="REVIEW_REQUIRED",
                lease_until=None,
                lease_token=None,
                error_code=None,
                result={
                    "artifact_sha256": artifact.digest(),
                    "artifact": artifact.model_dump(mode="json"),
                },
            )

    def _review_row(self, connection: Connection, job_id: str, principal: Principal) -> RowMapping:
        """Lock the job and current reviewer grant before exposing private OCR content."""
        row = (
            connection.execute(
                select(jobs)
                .where(
                    jobs.c.queue_id == self.queue_id,
                    jobs.c.job_id == job_id,
                    jobs.c.tenant_id == principal.tenant_id,
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ServiceError("job_not_found", "Ingestion job is unavailable", 404)
        source = IngestionInput.model_validate(row["input"])
        _authorize(principal, source)
        current = _current_publisher(connection, principal.subject, source)
        if current != principal:
            raise ServiceError("permission_changed", "Review permission has changed", 403)
        if row["state"] != "REVIEW_REQUIRED" or not (row["result"] or {}).get("artifact"):
            raise ServiceError("review_unavailable", "OCR review is unavailable", 409)
        return row

    def review_artifact(self, job_id: str, principal: Principal) -> OcrDocument:
        """Expose quarantined extraction only to a currently scoped administrator."""
        with self._transaction() as connection:
            row = self._review_row(connection, job_id, principal)
            return OcrDocument.model_validate(row["result"]["artifact"])

    def review_ocr(
        self,
        job_id: str,
        principal: Principal,
        review: OcrReview,
        catalog: SqlEvidenceCatalog,
    ) -> JobStatus:
        """Commit reviewed evidence and its decision together, or publish nothing on rejection."""
        review = OcrReview.model_validate(review.model_dump())
        with self._transaction() as connection:
            row = self._review_row(connection, job_id, principal)
            artifact = OcrDocument.model_validate(row["result"]["artifact"])
            if review.artifact_sha256 != artifact.digest():
                raise ServiceError("review_conflict", "OCR artifact has changed", 409)
            source = IngestionInput.model_validate(row["input"])
            _validate_ocr(source, artifact)
            result = dict(row["result"])
            result["review"] = review.model_dump(mode="json") | {
                "subject": principal.subject,
                "grant_revision": principal.revision,
            }
            if review.decision == "approve":
                _current_publisher(connection, row["subject"], source)
                batch = _reviewed_pages(artifact, review)
                _validate_extracted(source, batch)
                result.update(
                    catalog_id=catalog.catalog_id,
                    revision=catalog.publish_in_transaction(connection, batch),
                    physical_pages=len(batch),
                )
            self._change(
                connection,
                job_id,
                state="COMPLETED" if review.decision == "approve" else "FAILED",
                error_code=None if review.decision == "approve" else "review_rejected",
                result=result,
            )
            updated = (
                connection.execute(select(jobs).where(jobs.c.job_id == job_id)).mappings().one()
            )
            return _status(updated)


def _validate_ocr(source: IngestionInput, artifact: OcrDocument) -> None:
    """OCR changes extraction fields only; physical identity and permission scope stay fixed."""
    if source.parser != "ocr" or len(source.pages) != len(artifact.pages):
        raise ValueError("OCR artifact does not match the source manifest")
    mutable = {"text", "content_hash", "parser_version", "extraction_confidence"}
    for original, page in zip(source.pages, artifact.pages, strict=True):
        if page.pdf_sha256 != source.source_sha256 or original.model_dump(
            exclude=mutable
        ) != page.metadata.model_dump(exclude=mutable):
            raise ValueError("OCR artifact source or scope differs from the manifest")


def _reviewed_pages(artifact: OcrDocument, review: OcrReview) -> tuple[Page, ...]:
    """Human attestation satisfies admission; it is explicitly distinct from model confidence."""
    texts = review.corrected_text or tuple(page.metadata.text for page in artifact.pages)
    if len(texts) != len(artifact.pages):
        raise ServiceError(
            "invalid_review", "Corrections must contain every physical page in order", 422
        )
    return tuple(
        Page.model_validate(
            page.metadata.model_dump()
            | {
                "text": text,
                "content_hash": text_hash(text),
                "parser_version": page.parser + "-human-reviewed",
                "extraction_confidence": 1.0,
            }
        )
        for page, text in zip(artifact.pages, texts, strict=True)
    )
