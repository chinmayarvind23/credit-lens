"""Current grants and audit acknowledgments live outside eventually consistent indexes."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from creditlens.domain import Principal
from creditlens.errors import ServiceError

metadata = MetaData()
grants = Table(
    "principal_grants",
    metadata,
    Column("subject", String, primary_key=True),
    Column("tenant_id", String, nullable=False),
    Column("role", String, nullable=False),
    Column("borrower_ids", JSON, nullable=False),
    Column("acl_groups", JSON, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("enabled", Boolean, nullable=False),
)
audit_events = Table(
    "audit_events",
    metadata,
    Column("request_id", String, primary_key=True),
    Column("subject", String, nullable=False),
    Column("tenant_id", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("event", JSON, nullable=False),
)


def open_database(url: str) -> Engine:
    """SQLite supports local evidence; the same SQLAlchemy contract supports shared Postgres."""
    options: dict[str, Any] = {"pool_pre_ping": True}
    if url.startswith("postgresql+psycopg://"):
        options.update(
            pool_size=5,
            max_overflow=5,
            pool_timeout=5,
            connect_args={
                "connect_timeout": 3,
                "options": "-c statement_timeout=5000 -c lock_timeout=2000",
            },
        )
    if url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
        if url.endswith(":memory:"):
            options["poolclass"] = StaticPool
        elif url.startswith("sqlite:///"):
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **options)
    metadata.create_all(engine)
    return engine


class GrantStore:
    """Read grants for every request so JWT lifetime does not become permission lifetime."""

    def __init__(self, engine: Engine) -> None:
        """Share a connection pool, never a request transaction or cached permission result."""
        self.engine = engine

    def resolve(self, subject: str) -> Principal:
        """Unknown and disabled subjects share one denial to avoid an identity oracle."""
        with self.engine.connect() as connection:
            row = (
                connection.execute(select(grants).where(grants.c.subject == subject))
                .mappings()
                .first()
            )
        if row is None or not row["enabled"]:
            raise ServiceError("access_denied", "Access is not authorized", 403)
        return Principal.model_validate({key: row[key] for key in Principal.model_fields})

    def seed_demo(self) -> None:
        """Create a single public synthetic identity without overriding later revocation."""
        with self.engine.begin() as connection:
            exists = connection.execute(
                select(grants.c.subject).where(grants.c.subject == "synthetic-demo")
            ).first()
            if exists is None:
                connection.execute(
                    grants.insert().values(
                        subject="synthetic-demo",
                        tenant_id="demo-bank",
                        role="underwriter",
                        borrower_ids=[f"borrower-{i:03}" for i in range(1, 6)],
                        acl_groups=["underwriting"],
                        revision=1,
                        enabled=True,
                    )
                )

    def record(self, request_id: str, principal: Principal, event: dict[str, Any]) -> None:
        """A failed durable audit write prevents a successful query acknowledgment."""
        with self.engine.begin() as connection:
            connection.execute(
                audit_events.insert().values(
                    request_id=request_id,
                    subject=principal.subject,
                    tenant_id=principal.tenant_id,
                    created_at=datetime.now(UTC).isoformat(),
                    event=event,
                )
            )
