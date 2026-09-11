"""Actual PostgreSQL checks exercise shared authority, SQL filtering and writer serialization."""

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from hashlib import sha256
from threading import Barrier
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine, make_url

from creditlens.auth import authorized_page
from creditlens.domain import Page, Principal
from creditlens.errors import ServiceError
from creditlens.retrieval import chunk_page
from creditlens.sql_catalog import SqlEvidenceCatalog, initialize_catalog, pages, states


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Require the explicitly named local test database without resetting application data."""
    value = os.getenv("CREDITLENS_TEST_POSTGRES_URL")
    if not value:
        pytest.skip("Set CREDITLENS_TEST_POSTGRES_URL for actual PostgreSQL integration")
    url = make_url(value)
    if url.host not in {"127.0.0.1", "localhost"} or url.database != "creditlens_test":
        raise ValueError("Integration tests require the loopback creditlens_test database")
    result = create_engine(url, pool_size=4, max_overflow=0, connect_args={"connect_timeout": 3})
    try:
        yield result
    finally:
        result.dispose()


@pytest.fixture
def catalog(engine: Engine) -> SqlEvidenceCatalog:
    """Unique catalog IDs isolate every test without dropping shared tables or unrelated records."""
    return initialize_catalog(engine, "test-" + uuid4().hex)


def actor() -> Principal:
    """The trusted test principal can review only the named borrower and underwriting ACL."""
    return Principal(
        subject="test-reviewer",
        tenant_id="test-bank",
        role="underwriter",
        borrower_ids=("borrower-001",),
        acl_groups=("underwriting",),
        revision=1,
    )


def page(name: str = "financials", text: str = "Cash flow is 132000 USD.") -> Page:
    """Construct canonical synthetic text with a real content hash before publication."""
    return Page(
        tenant_id="test-bank",
        borrower_id="borrower-001",
        document_id=name,
        document_version="v1",
        page=1,
        document_kind="financial_summary",
        title=name,
        section="financial_summary",
        text=text,
        acl_groups=("underwriting",),
        valid_from=date(2026, 1, 1),
        content_hash=sha256(text.encode()).hexdigest(),
        parser_version="synthetic-test-v1",
    )


def test_requires_postgresql() -> None:
    """SQLite tests cannot establish the row-lock guarantees this implementation requires."""
    other = create_engine("sqlite://")
    try:
        for constructor in (initialize_catalog, SqlEvidenceCatalog):
            with pytest.raises(ValueError, match="PostgreSQL"):
                constructor(other)
    finally:
        other.dispose()


def test_explicit_bootstrap_and_missing_state(engine: Engine, catalog: SqlEvidenceCatalog) -> None:
    """Readers cannot create authority; deleted authority stays unavailable even when empty."""
    with pytest.raises(ServiceError, match="catalog_unavailable"):
        SqlEvidenceCatalog(engine, "absent-" + uuid4().hex)
    with pytest.raises(ValueError, match="named"):
        initialize_catalog(engine, "")
    with pytest.raises(ValueError, match="named"):
        SqlEvidenceCatalog(engine, "")
    with engine.begin() as connection:
        connection.execute(states.delete().where(states.c.catalog_id == catalog.catalog_id))
    with pytest.raises(ServiceError, match="catalog_unavailable"):
        catalog.snapshot(actor(), "borrower-001", date(2026, 6, 1))


def test_reconnect_idempotence_and_canonical_spans(
    engine: Engine, catalog: SqlEvidenceCatalog
) -> None:
    """New connections recover identical canonical chunks and their durable revision."""
    source = page(text="A financial paragraph.\n" * 140)
    assert catalog.publish((source,)) == 2
    version = catalog.version
    assert catalog.publish((source, source)) == 2
    assert catalog.version == version
    engine.dispose()
    reopened = initialize_catalog(engine, catalog.catalog_id)
    assert reopened.version == version
    candidates, revision = reopened.snapshot(actor(), "borrower-001", date(2026, 6, 1))
    assert set(candidates) == set(chunk_page(source))
    assert "".join(c.text for c in sorted(candidates, key=lambda c: c.start_char)) == source.text
    reopened.verify_revision(revision)


def test_conflict_rolls_back_whole_batch(engine: Engine, catalog: SqlEvidenceCatalog) -> None:
    """An immutable identity conflict cannot leave earlier new pages or an advanced epoch behind."""
    original = page()
    catalog.publish((original,))
    before = catalog.version
    with pytest.raises(ValueError, match="conflicts"):
        catalog.publish((page("new-page"), page(text="Contradictory financial text.")))
    assert catalog.version == before
    with engine.begin() as connection:
        records = connection.execute(
            select(pages.c.payload).where(pages.c.catalog_id == catalog.catalog_id)
        ).all()
    assert len(records) == 1
    assert records[0][0]["text"] == original.text


@pytest.mark.parametrize("case", ["empty", "too_many", "bad_hash", "oversized", "batch_bytes"])
def test_invalid_publication_changes_nothing(catalog: SqlEvidenceCatalog, case: str) -> None:
    """Reject invalid publication before a partial durable write or epoch change can occur."""
    source = page()
    batch = {
        "empty": (),
        "too_many": (source,) * 1001,
        "bad_hash": (source.model_copy(update={"content_hash": "forged"}),),
        "oversized": (page(text="x" * 1_048_577),),
        "batch_bytes": (page(text="x" * 1_048_576),) * 9,
    }[case]
    before = catalog.version
    with pytest.raises(ValueError):
        catalog.publish(batch)
    assert catalog.version == before


def test_shared_revocation_covers_siblings(engine: Engine, catalog: SqlEvidenceCatalog) -> None:
    """Another instance's revocation invalidates old snapshots and removes all sibling chunks."""
    catalog.publish((page(text="Cash flow schedule.\n" * 160),))
    second = SqlEvidenceCatalog(engine, catalog.catalog_id)
    candidates, revision = catalog.snapshot(actor(), "borrower-001", date(2026, 6, 1))
    assert len(candidates) > 1
    second.revoke(candidates[0].chunk_id)
    with pytest.raises(ServiceError, match="evidence_changed"):
        catalog.verify_revision(revision)
    assert catalog.snapshot(actor(), "borrower-001", date(2026, 6, 1))[0] == ()
    after = second.version
    second.revoke(candidates[0].chunk_id)
    assert second.version == after
    with pytest.raises(ValueError, match="Unknown"):
        second.revoke("unknown-chunk")
    assert second.version == after
    assert second.publish((page(text="Cash flow schedule.\n" * 160),)) == revision + 1
    assert second.snapshot(actor(), "borrower-001", date(2026, 6, 1))[0] == ()


def test_sql_scope_matches_authority(catalog: SqlEvidenceCatalog) -> None:
    """All tenant, borrower, ACL, confidence and half-open date predicates run before hydration."""
    variants = (
        page("allowed"),
        page("tenant").model_copy(update={"tenant_id": "other-bank"}),
        page("borrower").model_copy(update={"borrower_id": "borrower-002"}),
        page("restricted").model_copy(update={"acl_groups": ("credit-officer",)}),
        page("low-confidence").model_copy(update={"extraction_confidence": 0.899}),
        page("future").model_copy(update={"valid_from": date(2027, 1, 1)}),
        page("expired").model_copy(update={"valid_to": date(2026, 6, 1)}),
        page("boundary").model_copy(
            update={"extraction_confidence": 0.9, "valid_from": date(2026, 6, 1)}
        ),
        page("policy").model_copy(
            update={"borrower_id": None, "acl_groups": ("underwriting", "underwriting", "extra")}
        ),
    )
    catalog.publish(variants)
    today = date(2026, 6, 1)
    actual, _ = catalog.snapshot(actor(), "borrower-001", today)
    expected = tuple(
        c
        for p in variants
        if authorized_page(p, actor(), "borrower-001", today)
        for c in chunk_page(p)
    )
    assert set(actual) == set(expected)
    assert len(actual) == 3
    empty_acl = actor().model_copy(update={"acl_groups": ()})
    assert catalog.snapshot(empty_acl, "borrower-001", today)[0] == ()


def test_denied_borrower_never_touches_sql(engine: Engine, catalog: SqlEvidenceCatalog) -> None:
    """Even transaction setup is forbidden after a caller selects an unauthorized borrower."""
    calls = []

    def record(*args: object) -> None:
        """Capture execution presence without recording SQL parameters or source text."""
        calls.append(True)

    event.listen(engine, "before_cursor_execute", record)
    try:
        with pytest.raises(ServiceError, match="access_denied"):
            catalog.snapshot(actor(), "borrower-999", date(2026, 6, 1))
        assert calls == []
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_recreated_authority_cannot_replay_epoch(
    engine: Engine, catalog: SqlEvidenceCatalog
) -> None:
    """A new authority at the same integer epoch invalidates reads and verification."""
    _, revision = catalog.snapshot(actor(), "borrower-001", date(2026, 6, 1))
    with engine.begin() as connection:
        connection.execute(
            states.update()
            .where(states.c.catalog_id == catalog.catalog_id)
            .values(authority=str(uuid4()))
        )
    with pytest.raises(ServiceError, match="evidence_changed"):
        catalog.verify_revision(revision)
    with pytest.raises(ServiceError, match="evidence_changed"):
        catalog.snapshot(actor(), "borrower-001", date(2026, 6, 1))
    SqlEvidenceCatalog(engine, catalog.catalog_id).verify_revision(revision)


def test_concurrent_publication_has_no_lost_revision(
    engine: Engine, catalog: SqlEvidenceCatalog
) -> None:
    """Separate writer instances serialize on the durable state row and preserve both batches."""
    writers = [SqlEvidenceCatalog(engine, catalog.catalog_id) for _ in range(2)]
    barrier = Barrier(2)

    def publish(index: int) -> int:
        """Start independent database writes together instead of serializing them in the test."""
        barrier.wait(timeout=5)
        return writers[index].publish((page(f"concurrent-{index}"),))

    with ThreadPoolExecutor(max_workers=2) as pool:
        revisions = list(pool.map(publish, range(2)))
    assert sorted(revisions) == [2, 3]
    candidates, revision = catalog.snapshot(actor(), "borrower-001", date(2026, 6, 1))
    assert len(candidates) == 2 and revision == 3


def test_read_during_uncommitted_revoke_is_consistent(
    engine: Engine, catalog: SqlEvidenceCatalog
) -> None:
    """Readers see old data and epoch together, then reject them after revocation commits."""
    catalog.publish((page(),))
    candidates, revision = catalog.snapshot(actor(), "borrower-001", date(2026, 6, 1))
    with engine.begin() as connection:
        connection.execute(
            select(states).where(states.c.catalog_id == catalog.catalog_id).with_for_update()
        )
        connection.execute(
            pages.update().where(pages.c.catalog_id == catalog.catalog_id).values(revoked=True)
        )
        connection.execute(
            states.update()
            .where(states.c.catalog_id == catalog.catalog_id)
            .values(revision=revision + 1)
        )
        observed = SqlEvidenceCatalog(engine, catalog.catalog_id).snapshot(
            actor(), "borrower-001", date(2026, 6, 1)
        )
        assert observed == (candidates, revision)
    with pytest.raises(ServiceError, match="evidence_changed"):
        catalog.verify_revision(revision)
    assert catalog.snapshot(actor(), "borrower-001", date(2026, 6, 1)) == ((), revision + 1)


def test_lock_timeout_is_bounded_curated_and_recoverable(
    engine: Engine, catalog: SqlEvidenceCatalog
) -> None:
    """A blocked writer cannot hang indefinitely or leak SQL parameters in the public error."""
    before = catalog.version
    with engine.begin() as connection:
        connection.execute(
            select(states).where(states.c.catalog_id == catalog.catalog_id).with_for_update()
        )
        started = monotonic()
        with pytest.raises(ServiceError, match="catalog_unavailable") as failure:
            catalog.publish((page(text="private synthetic statement"),))
        assert monotonic() - started < 4
        assert "private synthetic statement" not in str(failure.value)
        assert "SELECT" not in str(failure.value)
    assert catalog.version == before
    assert catalog.publish((page(),)) == 2
