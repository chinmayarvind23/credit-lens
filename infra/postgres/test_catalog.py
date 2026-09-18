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
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine, make_url

from creditlens.api import create_app
from creditlens.auth import authorized_page
from creditlens.domain import Packet, Page, Principal
from creditlens.errors import ServiceError
from creditlens.retrieval import chunk_page
from creditlens.settings import Settings
from creditlens.sql_catalog import SqlEvidenceCatalog, initialize_catalog, pages, states
from creditlens.storage import audit_events, grants


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


def test_external_publication_requires_same_pool_active_transaction_and_isolation(
    engine: Engine, catalog: SqlEvidenceCatalog
) -> None:
    """Reject transaction misuse that could separate job completion from its catalog writes."""
    with engine.connect() as connection:
        with pytest.raises(ValueError):
            catalog.publish_in_transaction(connection, (page(),))
    with engine.execution_options(isolation_level="REPEATABLE READ").begin() as connection:
        with pytest.raises(ValueError):
            catalog.publish_in_transaction(connection, (page(),))
    other = create_engine("sqlite://")
    try:
        with other.begin() as connection:
            with pytest.raises(ValueError):
                catalog.publish_in_transaction(connection, (page(),))
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


def test_shared_runtime_requires_postgres_and_synthetic_namespace() -> None:
    """An opt-in catalog cannot silently use SQLite or an arbitrary production catalog name."""
    with pytest.raises(ValidationError, match="PostgreSQL"):
        Settings(catalog_backend="postgres")
    with pytest.raises(ValidationError, match="pattern"):
        Settings(demo_catalog_id="production")
    with pytest.raises(ValidationError, match="production mode"):
        Settings(governed_catalog_id="lender-catalog")
    with pytest.raises(ValidationError, match="governed catalog ID"):
        Settings(
            mode="production",
            catalog_backend="postgres",
            database_url="postgresql+psycopg://localhost/demo",
        )


def test_two_api_instances_share_catalog_cache_and_revocation(engine: Engine) -> None:
    """Actual PostgreSQL and optional actual Redis preserve scope and revocation across apps."""
    redis_url = os.getenv("CREDITLENS_TEST_REDIS_URL", "")
    config = Settings(
        database_url=engine.url.render_as_string(hide_password=False),
        catalog_backend="postgres",
        demo_catalog_id="synthetic-" + uuid4().hex,
        redis_url=SecretStr(redis_url),
        cache_signing_key=SecretStr("synthetic-cache-signing-test-key-32" if redis_url else ""),
    )
    body = {
        "borrower_id": "borrower-001",
        "question": "Calculate debt service coverage.",
        "effective_at": "2026-06-01",
    }
    with TestClient(create_app(config)) as first, TestClient(create_app(config)) as second:
        before = first.post("/api/v1/query", json=body)
        assert before.status_code == 200
        a = before.json()
        b = second.post("/api/v1/query", json=body).json()
        assert a["request_id"] != b["request_id"]
        assert a["corpus_version"] == b["corpus_version"]
        assert a["calculated_metrics"] == b["calculated_metrics"]
        if redis_url:
            assert not a["cache_hit"] and b["cache_hit"]
        target = next(c for c in a["evidence"] if c["section"] == "financial_summary")
        SqlEvidenceCatalog(engine, config.demo_catalog_id).revoke(target["chunk_id"])
        for client in (first, second):
            source = client.get(
                f"/api/v1/evidence/{target['chunk_id']}",
                params={"borrower_id": body["borrower_id"], "effective_at": body["effective_at"]},
            )
            assert source.status_code == 404
            packet = client.post("/api/v1/query", json=body).json()
            assert target["chunk_id"] not in {c["chunk_id"] for c in packet["evidence"]}
            assert packet["policy_disposition"] == "INSUFFICIENT_EVIDENCE"
        with TestClient(create_app(config)) as restarted:
            assert (
                restarted.get(
                    f"/api/v1/evidence/{target['chunk_id']}",
                    params={
                        "borrower_id": body["borrower_id"],
                        "effective_at": body["effective_at"],
                    },
                ).status_code
                == 404
            )
        with engine.connect() as connection:
            ids = set(connection.execute(select(audit_events.c.request_id)).scalars())
        assert {a["request_id"], b["request_id"]} <= ids
        try:
            with engine.begin() as connection:
                connection.execute(
                    grants.update()
                    .where(grants.c.subject == "synthetic-demo")
                    .values(enabled=False, revision=2)
                )
            assert first.post("/api/v1/query", json=body).status_code == 403
            assert second.post("/api/v1/query", json=body).status_code == 403
        finally:
            with engine.begin() as connection:
                connection.execute(
                    grants.update()
                    .where(grants.c.subject == "synthetic-demo")
                    .values(enabled=True, revision=1)
                )


def test_mid_query_sql_revocation_prevents_audit(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revocation committed by another instance during validation prevents a success audit."""
    from creditlens import workflow

    config = Settings(
        database_url=engine.url.render_as_string(hide_password=False),
        catalog_backend="postgres",
        demo_catalog_id="synthetic-" + uuid4().hex,
    )
    with TestClient(create_app(config)) as client:
        original = workflow.validate_packet
        writer = SqlEvidenceCatalog(engine, config.demo_catalog_id)

        def revoke(packet: Packet) -> None:
            """Commit revocation after packet construction and before final authorization."""
            original(packet)
            writer.revoke(packet.evidence[0].chunk_id)

        monkeypatch.setattr(workflow, "validate_packet", revoke)
        with engine.connect() as connection:
            before = set(connection.execute(select(audit_events.c.request_id)).scalars())
        response = client.post(
            "/api/v1/query",
            json={
                "borrower_id": "borrower-001",
                "question": "Calculate DSCR.",
                "effective_at": "2026-06-01",
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "evidence_changed"
        with engine.connect() as connection:
            after = set(connection.execute(select(audit_events.c.request_id)).scalars())
        assert before == after


def test_readiness_rejects_replaced_catalog_authority(engine: Engine) -> None:
    """A live database connection must not hide an invalidated workflow catalog identity."""
    config = Settings(
        database_url=engine.url.render_as_string(hide_password=False),
        catalog_backend="postgres",
        demo_catalog_id="synthetic-" + uuid4().hex,
    )
    with TestClient(create_app(config)) as client:
        assert client.get("/ready").status_code == 200
        with engine.begin() as connection:
            connection.execute(
                states.update()
                .where(states.c.catalog_id == config.demo_catalog_id)
                .values(authority=str(uuid4()))
            )
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "catalog_unavailable"


def test_grounded_provider_rechecks_shared_name_and_grants(engine, catalog):
    """A separate catalog reader can invalidate a name-grounded result across SQL authority."""
    from creditlens.citations import cite
    from creditlens.corpus import build_demo_pages
    from creditlens.domain import QueryRequest
    from creditlens.local_search import LocalSearchProvider
    from creditlens.query_grounding import GroundedProvider
    from creditlens.storage import GrantStore, metadata

    metadata.create_all(engine)
    principal = Principal(
        subject=f"grounding-{uuid4()}",
        tenant_id="demo-bank",
        role="underwriter",
        borrower_ids=("borrower-001",),
        acl_groups=("underwriting",),
        revision=1,
    )
    with engine.begin() as connection:
        connection.execute(grants.insert().values(**principal.model_dump(), enabled=True))
    catalog.publish(build_demo_pages())
    provider = GroundedProvider(
        LocalSearchProvider(catalog, GrantStore(engine)), GrantStore(engine)
    )
    request = QueryRequest(
        borrower_id="borrower-001",
        question="Which package inputs are missing?",
        effective_at=date(2026, 9, 11),
    )
    result = provider.search(request, principal)
    assert result.request == request and result.source.request.question.startswith(
        "Borrower: Northstar"
    )
    assert provider.citation(result, cite(result.chunks[0])) == result.chunks[0]
    other = SqlEvidenceCatalog(engine, catalog.catalog_id)
    allowed, _ = other.snapshot(principal, request.borrower_id, request.effective_at)
    other.revoke(next(c.chunk_id for c in allowed if c.document_kind == "application"))
    with pytest.raises(ServiceError):
        provider.verify(result)
    with engine.begin() as connection:
        connection.execute(
            grants.update().where(grants.c.subject == principal.subject).values(revision=2)
        )
    with pytest.raises(ServiceError, match="access_changed"):
        provider.search(request, principal)


def test_governed_cortex_runtime_uses_existing_catalog(engine, monkeypatch):
    """Real SQL and signed-token HTTP exercise runtime wiring around a declared Cortex double."""
    import json
    from datetime import UTC, datetime, timedelta

    import httpx
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from pydantic import SecretStr

    import creditlens.runtime as runtime
    from creditlens.corpus import build_demo_pages
    from creditlens.cortex_search import index_record
    from creditlens.retrieval import lexical_rank
    from creditlens.storage import metadata

    metadata.create_all(engine)
    catalog = initialize_catalog(engine, "governed-" + uuid4().hex)
    catalog.publish(build_demo_pages())
    principal = Principal(
        subject="governed-" + uuid4().hex,
        tenant_id="demo-bank",
        role="underwriter",
        borrower_ids=("borrower-001",),
        acl_groups=("underwriting",),
        revision=1,
    )
    with engine.begin() as connection:
        connection.execute(grants.insert().values(**principal.model_dump(), enabled=True))
    endpoint = "https://example.snowflakecomputing.com/api/v2/databases/DB/schemas/PUBLIC/"
    endpoint += "cortex-search-services/EVIDENCE:query"
    settings = Settings(
        mode="production",
        database_url=engine.url.render_as_string(hide_password=False),
        catalog_backend="postgres",
        governed_catalog_id=catalog.catalog_id,
        generation_model="qwen3:8b",
        generation_digest="a" * 64,
        issuer="https://cognito-idp.us-east-1.amazonaws.com/fixture",
        client_id="fixture",
        cortex_url=endpoint,
        cortex_token=SecretStr("test-only"),
        response_cache_enabled=True,
    )
    calls, clients = [], []

    def transport(request):
        """Emulate only remote ranking; canonical SQL, grant checks and audit remain real."""
        calls.append(json.loads(request.content))
        assert str(request.url) == endpoint
        assert request.headers["Authorization"] == "Bearer test-only"
        candidates, _ = catalog.snapshot(principal, "borrower-001", date(2026, 6, 1))
        ranked = lexical_rank(calls[-1]["query"], candidates)[:10]
        return httpx.Response(200, json={"results": [index_record(c) for c in ranked]})

    def client_factory(**kwargs):
        """Keep actual HTTP client lifetime while preventing any external service request."""
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        client = httpx.Client(transport=httpx.MockTransport(transport), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(runtime, "ProviderClient", client_factory)
    from contextlib import nullcontext

    # Keep this SQL/Cortex fixture independent of the dedicated generation contract/live suites.
    monkeypatch.setattr(runtime, "open_generation", lambda config: nullcontext(None))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": principal.subject,
            "iss": settings.issuer,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "token_use": "access",
            "client_id": settings.client_id,
            "scope": settings.required_scope,
        },
        key,
        algorithm="RS256",
    )
    app = create_app(settings)
    body = {
        "borrower_id": "borrower-001",
        "question": "Calculate DSCR",
        "effective_at": "2026-06-01",
    }
    initial_revision = catalog.version
    with TestClient(app) as client:
        app.state.auth.key_resolver = lambda token: key.public_key()
        assert catalog.version == initial_revision
        assert client.post("/api/v1/query", json=body).status_code == 401
        client.headers["Authorization"] = f"Bearer {token}"
        first = client.post("/api/v1/query", json=body)
        assert first.status_code == 200, first.text
        packet = Packet.model_validate(first.json())
        assert packet.provider_mode == "local-extractive"
        assert str(packet.calculated_metrics[0].value) == "1.5000"
        repeated = Packet.model_validate(client.post("/api/v1/query", json=body).json())
        assert repeated.cache_hit and repeated.request_id != packet.request_id
        assert len(calls) == 1 and "filter" in calls[0]
        source = packet.evidence[0]
        catalog.revoke(source.chunk_id)
        denied_source = client.get(
            f"/api/v1/evidence/{source.chunk_id}",
            params={"borrower_id": "borrower-001", "effective_at": "2026-06-01"},
        )
        assert denied_source.status_code in (403, 404)
        with engine.begin() as connection:
            connection.execute(
                grants.update()
                .where(grants.c.subject == principal.subject)
                .values(enabled=False, revision=2)
            )
        assert client.post("/api/v1/query", json=body).status_code == 403
        with engine.connect() as connection:
            ids = set(
                connection.execute(
                    select(audit_events.c.request_id).where(
                        audit_events.c.subject == principal.subject
                    )
                ).scalars()
            )
        assert ids == {packet.request_id, repeated.request_id}
        with engine.connect() as connection:
            event = connection.execute(
                select(audit_events.c.event).where(audit_events.c.request_id == packet.request_id)
            ).scalar_one()
        assert event["search_provider_mode"] == "snowflake-cortex-rest"
    assert clients and all(c.is_closed for c in clients)
