"""PostgreSQL owns canonical evidence and revocation epochs shared across processes."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    Float,
    ForeignKeyConstraint,
    MetaData,
    String,
    Table,
    and_,
    exists,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.elements import ColumnElement

from creditlens.auth import authorize_borrower
from creditlens.domain import Chunk, Page, Principal
from creditlens.errors import ServiceError
from creditlens.retrieval import chunk_page

schema = MetaData()
states = Table(
    "evidence_catalog_state",
    schema,
    Column("catalog_id", String, primary_key=True),
    Column("authority", String, nullable=False),
    Column("revision", BigInteger, nullable=False),
)
pages = Table(
    "evidence_pages",
    schema,
    Column("catalog_id", String, primary_key=True),
    Column("page_key", String, primary_key=True),
    Column("tenant_id", String, nullable=False, index=True),
    Column("borrower_id", String),
    Column("valid_from", Date, nullable=False),
    Column("valid_to", Date),
    Column("confidence", Float, nullable=False),
    Column("revoked", Boolean, nullable=False),
    Column("payload", JSON, nullable=False),
    ForeignKeyConstraint(["catalog_id"], ["evidence_catalog_state.catalog_id"]),
)
chunks = Table(
    "evidence_chunks",
    schema,
    Column("catalog_id", String, primary_key=True),
    Column("chunk_id", String, primary_key=True),
    Column("page_key", String, nullable=False),
    Column("payload", JSON, nullable=False),
    ForeignKeyConstraint(
        ["catalog_id", "page_key"], ["evidence_pages.catalog_id", "evidence_pages.page_key"]
    ),
)
page_acls = Table(
    "evidence_page_acls",
    schema,
    Column("catalog_id", String, primary_key=True),
    Column("page_key", String, primary_key=True),
    Column("acl_group", String, primary_key=True),
    ForeignKeyConstraint(
        ["catalog_id", "page_key"], ["evidence_pages.catalog_id", "evidence_pages.page_key"]
    ),
)


def page_identity(page: Page) -> str:
    """Bind immutable physical-page identity separately from content and chunk boundaries."""
    value = [page.tenant_id, page.document_id, page.document_version, page.page]
    return sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def initialize_catalog(engine: Engine, catalog_id: str = "default") -> "SqlEvidenceCatalog":
    """Explicitly bootstrap the owned schema; ordinary readers do not gain migration behavior."""
    if engine.dialect.name != "postgresql" or not catalog_id:
        raise ValueError("A named PostgreSQL catalog is required")
    schema.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(states)
            .values(catalog_id=catalog_id, authority=str(uuid4()), revision=1)
            .on_conflict_do_nothing(index_elements=[states.c.catalog_id])
        )
    return SqlEvidenceCatalog(engine, catalog_id)


class SqlEvidenceCatalog:
    """Serialize writers and use one SQL statement snapshot for scoped evidence plus its epoch."""

    def __init__(self, engine: Engine, catalog_id: str = "default") -> None:
        """Capture authority identity so a recreated catalog cannot replay an old integer epoch."""
        if engine.dialect.name != "postgresql" or not catalog_id:
            raise ValueError("A named PostgreSQL catalog is required")
        self.engine = engine.execution_options(isolation_level="READ COMMITTED")
        self.catalog_id = catalog_id
        self._authority: str | None = None
        with self._transaction() as connection:
            self._state(connection)

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        """Bound lock and statement waits; callers retain explicit retry and failure control."""
        try:
            with self.engine.begin() as connection:
                connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
                connection.exec_driver_sql("SET LOCAL lock_timeout = '2s'")
                yield connection
        except SQLAlchemyError as error:
            raise ServiceError(
                "catalog_unavailable", "Evidence catalog is unavailable", 503
            ) from error

    def _state(self, connection: Connection, *, lock: bool = False) -> int:
        """Fresh authority and epoch checks detect both revocation and catalog recreation."""
        statement = select(states).where(states.c.catalog_id == self.catalog_id)
        if lock:
            statement = statement.with_for_update()
        row = connection.execute(statement).mappings().first()
        if row is None:
            raise ServiceError("catalog_unavailable", "Evidence catalog is unavailable", 503)
        authority = str(row["authority"])
        if self._authority is not None and authority != self._authority:
            raise ServiceError("evidence_changed", "Evidence changed; retry the request", 409)
        self._authority = authority
        return int(row["revision"])

    @property
    def version(self) -> str:
        """Expose durable identity and current epoch for cache and audit provenance."""
        with self._transaction() as connection:
            revision = self._state(connection)
        return f"sql-v1:{self._authority}:{revision}"

    def verify_revision(self, revision: int) -> None:
        """Re-read committed state instead of trusting process-local or Redis revocation state."""
        with self._transaction() as connection:
            if self._state(connection) != revision:
                raise ServiceError("evidence_changed", "Evidence changed; retry the request", 409)

    def publish(self, batch: tuple[Page, ...]) -> int:
        """Commit an immutable batch atomically; duplicates are no-ops and conflicts roll back."""
        if not batch or len(batch) > 1000:
            raise ValueError("Publish requires between 1 and 1000 physical pages")
        if sum(len(page.text.encode()) for page in batch) > 8_388_608:
            raise ValueError("Canonical publication text exceeds the 8 MiB batch limit")
        for page in batch:
            encoded = page.text.encode()
            if len(encoded) > 1_048_576 or sha256(encoded).hexdigest() != page.content_hash:
                raise ValueError("Canonical page text must match its bounded content hash")
        with self._transaction() as connection:
            revision = self._state(connection, lock=True)
            added = sum(self._publish_page(connection, page) for page in batch)
            return self._advance(connection, revision) if added else revision

    def _publish_page(self, connection: Connection, page: Page) -> bool:
        """Generate canonical chunks only after confirming the page's immutable identity."""
        key = page_identity(page)
        payload = page.model_dump(mode="json")
        predicate = and_(pages.c.catalog_id == self.catalog_id, pages.c.page_key == key)
        existing = connection.execute(select(pages.c.payload).where(predicate)).scalar_one_or_none()
        if existing is not None:
            if existing != payload:
                raise ValueError("Immutable page identity conflicts with existing content")
            return False
        connection.execute(
            pages.insert().values(
                catalog_id=self.catalog_id,
                page_key=key,
                tenant_id=page.tenant_id,
                borrower_id=page.borrower_id,
                valid_from=page.valid_from,
                valid_to=page.valid_to,
                confidence=page.extraction_confidence,
                revoked=False,
                payload=payload,
            )
        )
        connection.execute(
            page_acls.insert(),
            [
                {"catalog_id": self.catalog_id, "page_key": key, "acl_group": group}
                for group in sorted(set(page.acl_groups))
            ],
        )
        connection.execute(
            chunks.insert(),
            [
                {
                    "catalog_id": self.catalog_id,
                    "page_key": key,
                    "chunk_id": chunk.chunk_id,
                    "payload": chunk.model_dump(mode="json"),
                }
                for chunk in chunk_page(page)
            ],
        )
        return True

    def _advance(self, connection: Connection, revision: int) -> int:
        """The held catalog row lock prevents lost increments from concurrent writers."""
        connection.execute(
            states.update()
            .where(states.c.catalog_id == self.catalog_id)
            .values(revision=revision + 1)
        )
        return revision + 1

    def revoke(self, chunk_id: str) -> None:
        """Revoke every chunk on the source page; repeating the same operation is idempotent."""
        with self._transaction() as connection:
            revision = self._state(connection, lock=True)
            key = connection.execute(
                select(chunks.c.page_key).where(
                    chunks.c.catalog_id == self.catalog_id, chunks.c.chunk_id == chunk_id
                )
            ).scalar_one_or_none()
            if key is None:
                raise ValueError("Unknown evidence chunk")
            changed = connection.execute(
                pages.update()
                .where(
                    pages.c.catalog_id == self.catalog_id,
                    pages.c.page_key == key,
                    pages.c.revoked.is_(False),
                )
                .values(revoked=True)
            ).rowcount
            if changed:
                self._advance(connection, revision)

    def snapshot(
        self, principal: Principal, borrower_id: str, effective_at: date
    ) -> tuple[tuple[Chunk, ...], int]:
        """Reject unauthorized borrower scope before SQL, then filter all page scope in SQL."""
        authorize_borrower(principal, borrower_id)
        allowed = self._allowed(principal, borrower_id, effective_at)
        page_chunks = chunks.join(
            pages,
            and_(chunks.c.catalog_id == pages.c.catalog_id, chunks.c.page_key == pages.c.page_key),
        )
        statement = (
            select(states.c.authority, states.c.revision, chunks.c.payload)
            .select_from(
                states.outerjoin(
                    page_chunks, and_(chunks.c.catalog_id == states.c.catalog_id, allowed)
                )
            )
            .where(states.c.catalog_id == self.catalog_id)
            .order_by(chunks.c.chunk_id)
        )
        with self._transaction() as connection:
            rows = connection.execute(statement).mappings().all()
        if not rows:
            raise ServiceError("catalog_unavailable", "Evidence catalog is unavailable", 503)
        if rows[0]["authority"] != self._authority:
            raise ServiceError("evidence_changed", "Evidence changed; retry the request", 409)
        result = tuple(Chunk.model_validate(row["payload"]) for row in rows if row["payload"])
        return result, int(rows[0]["revision"])

    def _allowed(
        self, principal: Principal, borrower_id: str, effective_at: date
    ) -> ColumnElement[bool]:
        """ACL existence avoids duplicate rows and filters forbidden evidence before Python."""
        group_match = exists(
            select(page_acls.c.page_key).where(
                page_acls.c.catalog_id == pages.c.catalog_id,
                page_acls.c.page_key == pages.c.page_key,
                page_acls.c.acl_group.in_(principal.acl_groups),
            )
        )
        return and_(
            pages.c.tenant_id == principal.tenant_id,
            or_(pages.c.borrower_id.is_(None), pages.c.borrower_id == borrower_id),
            pages.c.valid_from <= effective_at,
            or_(pages.c.valid_to.is_(None), pages.c.valid_to > effective_at),
            pages.c.confidence >= 0.9,
            pages.c.revoked.is_(False),
            group_match,
        )
