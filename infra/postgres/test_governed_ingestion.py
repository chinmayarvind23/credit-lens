"""Real PostgreSQL checks bind governed work to current catalog and publisher authority."""

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from hashlib import sha256
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from creditlens.api import create_app, current_principal
from creditlens.domain import Page, Principal
from creditlens.errors import ServiceError
from creditlens.ingestion_context import ingestion_context
from creditlens.ingestion_jobs import (
    IngestionInput,
    JobStore,
    initialize_jobs,
    jobs,
    queue_bindings,
)
from creditlens.ocr import OcrDocument, OcrReview, normalize_vl
from creditlens.settings import Settings
from creditlens.sql_catalog import (
    SqlEvidenceCatalog,
    chunks,
    initialize_catalog,
    page_acls,
    pages,
    states,
)
from creditlens.storage import GrantStore, grants, open_database
from scripts.ingest_documents import execute


@pytest.fixture
def scope():
    """Keep every row uniquely owned and require an explicit disposable PostgreSQL database."""
    url = os.environ.get("CREDITLENS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set CREDITLENS_TEST_POSTGRES_URL for governed ingestion integration")
    parsed = make_url(url)
    if parsed.host not in {"127.0.0.1", "localhost"} or parsed.database != "creditlens_test":
        raise ValueError("Governed ingestion tests require loopback creditlens_test")
    engine = open_database(url)
    initialize_jobs(engine)
    identity = "governed-" + uuid4().hex
    catalog = initialize_catalog(engine, identity)
    other = initialize_catalog(engine, identity + "-other")
    actor = Principal(
        subject=identity,
        tenant_id=identity,
        role="admin",
        borrower_ids=("account-1",),
        acl_groups=("credit",),
        revision=1,
    )
    with engine.begin() as connection:
        connection.execute(grants.insert().values(**actor.model_dump(mode="json"), enabled=True))
    try:
        yield SimpleNamespace(
            engine=engine,
            url=url,
            catalog=catalog,
            other=other,
            actor=actor,
            queue=identity,
            tenant=identity,
        )
    finally:
        with engine.begin() as connection:
            connection.execute(jobs.delete().where(jobs.c.queue_id.startswith(identity)))
            connection.execute(
                queue_bindings.delete().where(queue_bindings.c.queue_id.startswith(identity))
            )
            for table in (chunks, page_acls, pages, states):
                connection.execute(
                    table.delete().where(
                        table.c.catalog_id.in_([catalog.catalog_id, other.catalog_id])
                    )
                )
            connection.execute(grants.delete().where(grants.c.subject == actor.subject))
        engine.dispose()


def source(scope, document="source-1"):
    """Use explicit governed metadata and canonical text without loading demo fixtures."""
    text = "Annual operating cash flow is USD 180000.00."
    page = Page(
        tenant_id=scope.tenant,
        borrower_id="account-1",
        document_id=document,
        document_version="v1",
        page=1,
        document_kind="financial_statement",
        title=document,
        section="Cash flow",
        text=text,
        acl_groups=("credit",),
        valid_from=date(2026, 1, 1),
        content_hash=sha256(text.encode()).hexdigest(),
        parser_version="test-extractor",
    )
    return IngestionInput(source_sha256="a" * 64, pages=(page,), parser="digital")


def config(scope, **changes):
    """Configure production without starting its unreachable model/search endpoints."""
    values = dict(
        mode="production",
        database_url=scope.url,
        catalog_backend="postgres",
        governed_catalog_id=scope.catalog.catalog_id,
        ingestion_enabled=True,
        ingestion_queue_id=scope.queue,
        issuer="https://cognito-idp.us-east-1.amazonaws.com/test",
        client_id="test-client",
        cortex_url="https://example.invalid/search",
        cortex_token=SecretStr("test-only"),
        generation_model="fixture:local",
        generation_digest="a" * 64,
    )
    return Settings(**(values | changes))


def count_jobs(scope):
    """Observe committed intent independently of the JobStore response."""
    with scope.engine.connect() as connection:
        return connection.scalar(
            select(func.count()).select_from(jobs).where(jobs.c.queue_id == scope.queue)
        )


def test_constructor_rejects_foreign_pool_before_binding_write(scope):
    """Even the same database URL cannot mix separately owned transaction pools."""
    other_engine = open_database(scope.url)
    try:
        catalog = SqlEvidenceCatalog(other_engine, scope.catalog.catalog_id)
        with pytest.raises(ValueError, match="same database pool"):
            JobStore(scope.engine, scope.queue, catalog=catalog)
        with scope.engine.connect() as connection:
            assert (
                connection.scalar(
                    select(queue_bindings.c.queue_id).where(
                        queue_bindings.c.queue_id == scope.queue
                    )
                )
                is None
            )
        assert count_jobs(scope) == 0
    finally:
        other_engine.dispose()


def test_bound_queue_reopens_only_with_its_exact_catalog(scope):
    """Catalog identity is durable and cannot become another catalog or an unbound queue."""
    JobStore(scope.engine, scope.queue, catalog=scope.catalog)
    JobStore(
        scope.engine,
        scope.queue,
        catalog=SqlEvidenceCatalog(scope.engine, scope.catalog.catalog_id),
    )
    for catalog in (scope.other, None):
        with pytest.raises(ServiceError, match="ingestion_catalog_mismatch"):
            JobStore(scope.engine, scope.queue, catalog=catalog)
    with scope.engine.connect() as connection:
        row = (
            connection.execute(
                select(queue_bindings).where(queue_bindings.c.queue_id == scope.queue)
            )
            .mappings()
            .one()
        )
    assert (row["catalog_id"], row["authority"]) == (
        scope.catalog.catalog_id,
        scope.catalog.authority_id,
    )


@pytest.mark.parametrize("catalog_id,authority", [("catalog", None), (None, "authority")])
def test_database_rejects_partial_binding_pairs(scope, catalog_id, authority):
    """Direct SQL cannot persist half of an immutable authority pair."""
    with pytest.raises(IntegrityError):
        with scope.engine.begin() as connection:
            connection.execute(
                queue_bindings.insert().values(
                    queue_id=scope.queue, catalog_id=catalog_id, authority=authority
                )
            )


@pytest.mark.parametrize("mutation", ["missing", "recreated", "deleted-catalog"])
def test_existing_worker_rejects_lost_catalog_authority(scope, mutation):
    """A captured binding cannot authorize work after durable identity changes."""
    store = JobStore(scope.engine, scope.queue, catalog=scope.catalog)
    with scope.engine.begin() as connection:
        if mutation == "missing":
            connection.execute(
                queue_bindings.delete().where(queue_bindings.c.queue_id == scope.queue)
            )
        elif mutation == "recreated":
            connection.execute(
                states.update()
                .where(states.c.catalog_id == scope.catalog.catalog_id)
                .values(authority=str(uuid4()))
            )
        else:
            connection.execute(
                states.delete().where(states.c.catalog_id == scope.catalog.catalog_id)
            )
    with pytest.raises(ServiceError, match="ingestion_catalog_mismatch"):
        store.submit(source(scope), scope.actor, "late")
    assert count_jobs(scope) == 0
    if mutation == "recreated":
        with pytest.raises(ServiceError, match="ingestion_catalog_mismatch"):
            JobStore(
                scope.engine,
                scope.queue,
                catalog=SqlEvidenceCatalog(scope.engine, scope.catalog.catalog_id),
            )


@pytest.mark.parametrize("legacy_row", [False, True])
def test_governed_queue_cannot_adopt_legacy_work(scope, legacy_row):
    """Both explicit unbound queues and old jobs with no binding need a fresh governed queue."""
    legacy = JobStore(scope.engine, scope.queue)
    job = legacy.submit(source(scope), scope.actor, "old")
    if legacy_row:
        with scope.engine.begin() as connection:
            connection.execute(
                queue_bindings.delete().where(queue_bindings.c.queue_id == scope.queue)
            )
    with pytest.raises(ServiceError, match="ingestion_catalog_mismatch"):
        JobStore(scope.engine, scope.queue, catalog=scope.catalog)
    with scope.engine.connect() as connection:
        assert (
            connection.scalar(select(jobs.c.state).where(jobs.c.job_id == job.job_id)) == "QUEUED"
        )


def test_empty_unbound_queue_cannot_be_reinterpreted_as_governed(scope):
    """An explicit null binding remains immutable even before its first job arrives."""
    JobStore(scope.engine, scope.queue)
    with pytest.raises(ServiceError, match="ingestion_catalog_mismatch"):
        JobStore(scope.engine, scope.queue, catalog=scope.catalog)
    with scope.engine.connect() as connection:
        row = (
            connection.execute(
                select(queue_bindings).where(queue_bindings.c.queue_id == scope.queue)
            )
            .mappings()
            .one()
        )
    assert row["catalog_id"] is None and row["authority"] is None
    assert count_jobs(scope) == 0


def test_wrong_catalog_ocr_review_preserves_quarantine_and_catalogs(scope):
    """A valid review cannot publish or mutate an artifact through another catalog."""
    store = JobStore(scope.engine, scope.queue, catalog=scope.catalog)
    spec = source(scope).model_copy(update={"parser": "ocr"})
    raw = json.dumps(
        {
            "width": 100,
            "height": 100,
            "parsing_res_list": [
                {
                    "block_label": "text",
                    "block_content": spec.pages[0].text,
                    "block_bbox": [0, 0, 100, 100],
                    "block_order": 1,
                }
            ],
        }
    ).encode()
    artifact = OcrDocument(
        pages=(
            normalize_vl(
                raw,
                spec.pages[0],
                pdf_sha256=spec.source_sha256,
                image_sha256="b" * 64,
                models_sha256="c" * 64,
                generation_complete=True,
            ),
        )
    )
    job = store.submit(spec, scope.actor, "ocr")
    lease = store.claim(job.job_id, parser="ocr")
    assert lease is not None
    store.stage_ocr(lease, artifact)
    with scope.engine.connect() as connection:
        before = dict(
            connection.execute(select(jobs).where(jobs.c.job_id == job.job_id)).mappings().one()
        )
    review = OcrReview(artifact_sha256=artifact.digest(), decision="approve", reason="Checked")
    with pytest.raises(ServiceError, match="ingestion_catalog_mismatch"):
        store.review_ocr(job.job_id, scope.actor, review, scope.other)
    with scope.engine.connect() as connection:
        after = dict(
            connection.execute(select(jobs).where(jobs.c.job_id == job.job_id)).mappings().one()
        )
    assert after == before
    assert store.review_artifact(job.job_id, scope.actor) == artifact
    for catalog in (scope.catalog, scope.other):
        assert catalog.snapshot(scope.actor, "account-1", date(2026, 6, 1))[0] == ()


def test_wrong_catalog_completion_rolls_back_job_and_pages(scope):
    """Valid source and publisher do not authorize publication into another catalog."""
    store = JobStore(scope.engine, scope.queue, catalog=scope.catalog)
    spec = source(scope)
    job = store.submit(spec, scope.actor, "publish")
    lease = store.claim(job.job_id)
    assert lease is not None
    with pytest.raises(ServiceError, match="ingestion_catalog_mismatch"):
        store.complete(lease, scope.other, spec.pages)
    assert store.status(job.job_id, scope.actor).state == "RUNNING"
    for catalog in (scope.catalog, scope.other):
        assert catalog.snapshot(scope.actor, "account-1", date(2026, 6, 1))[0] == ()


@pytest.mark.parametrize("competing", [False, True])
def test_concurrent_queue_initialization_has_one_durable_authority(scope, competing):
    """The primary-key race permits identical reopen but rejects a competing catalog binding."""
    barrier = Barrier(2)

    def open_queue(catalog):
        """Start two real transactions before either has initialized the queue."""
        barrier.wait(timeout=5)
        try:
            JobStore(scope.engine, scope.queue, catalog=catalog)
            return "opened"
        except ServiceError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(open_queue, [scope.catalog, scope.other if competing else scope.catalog])
        )
    assert sorted(outcomes) == (
        ["ingestion_catalog_mismatch", "opened"] if competing else ["opened", "opened"]
    )
    with scope.engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(queue_bindings)
                .where(queue_bindings.c.queue_id == scope.queue)
            )
            == 1
        )


def test_two_workers_hold_binding_and_publish_distinct_jobs(scope, monkeypatch):
    """Both workers enter publication before the catalog serializes their writes."""
    store = JobStore(scope.engine, scope.queue, catalog=scope.catalog)
    specs = [source(scope, "first"), source(scope, "second")]
    ids = [store.submit(spec, scope.actor, spec.pages[0].document_id).job_id for spec in specs]
    barrier = Barrier(2)
    publish = SqlEvidenceCatalog.publish_in_transaction

    def simultaneous_publication(catalog, connection, batch):
        """An exclusive queue lock would prevent the second worker reaching this barrier."""
        barrier.wait(timeout=4)
        return publish(catalog, connection, batch)

    def complete_one(item):
        """Use an independent worker object and current lease for one durable job."""
        job_id, spec = item
        catalog = SqlEvidenceCatalog(scope.engine, scope.catalog.catalog_id)
        worker = JobStore(scope.engine, scope.queue, catalog=catalog)
        lease = worker.claim(job_id)
        assert lease is not None
        worker.complete(lease, catalog, spec.pages)
        return worker.status(job_id, scope.actor).state

    monkeypatch.setattr(SqlEvidenceCatalog, "publish_in_transaction", simultaneous_publication)
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(complete_one, zip(ids, specs, strict=True))) == [
            "COMPLETED",
            "COMPLETED",
        ]
    found, revision = scope.catalog.snapshot(scope.actor, "account-1", date(2026, 6, 1))
    assert {chunk.document_id for chunk in found} == {"first", "second"}
    assert revision == 3


@pytest.mark.parametrize("change", [{"enabled": False}, {"revision": 2}, {"borrower_ids": []}])
def test_stale_or_revoked_submitter_cannot_insert_intent(scope, change):
    """Rechecking the grant in the submission transaction blocks stale authenticated snapshots."""
    store = JobStore(scope.engine, scope.queue, catalog=scope.catalog)
    with scope.engine.begin() as connection:
        connection.execute(
            grants.update().where(grants.c.subject == scope.actor.subject).values(**change)
        )
    with pytest.raises(ServiceError):
        store.submit(source(scope), scope.actor, "revoked")
    assert count_jobs(scope) == 0


def test_revoked_idempotent_retry_does_not_expose_existing_job(scope):
    """Duplicate intent still requires current authority rather than returning a stale success."""
    store = JobStore(scope.engine, scope.queue, catalog=scope.catalog)
    spec = source(scope)
    store.submit(spec, scope.actor, "same")
    with scope.engine.begin() as connection:
        connection.execute(
            grants.update().where(grants.c.subject == scope.actor.subject).values(enabled=False)
        )
    with pytest.raises(ServiceError, match="permission_changed"):
        store.submit(spec, scope.actor, "same")
    assert count_jobs(scope) == 1


def test_production_context_uses_existing_governed_catalog_only(scope):
    """A deliberately absent demo catalog must not affect production context construction."""
    settings = config(scope, demo_catalog_id="synthetic-absent")
    store, catalog = ingestion_context(settings, scope.engine)
    assert catalog.catalog_id == scope.catalog.catalog_id
    assert store.queue_id == scope.queue
    with scope.engine.connect() as connection:
        assert (
            connection.scalar(
                select(states.c.catalog_id).where(states.c.catalog_id == settings.demo_catalog_id)
            )
            is None
        )
    with pytest.raises(ServiceError, match="catalog_unavailable"):
        ingestion_context(config(scope, governed_catalog_id="missing-" + uuid4().hex), scope.engine)


def test_production_ingestion_requires_explicit_queue_but_remains_optional(scope):
    """A disabled path keeps normal serving configuration valid without default-queue adoption."""
    assert not config(
        scope, ingestion_enabled=False, ingestion_queue_id="synthetic-ingestion-v1"
    ).ingestion_enabled
    with pytest.raises(ValidationError, match="governed catalog and queue"):
        config(scope, ingestion_queue_id="synthetic-ingestion-v1")
    with pytest.raises(ValueError, match="Enable"):
        ingestion_context(config(scope, ingestion_enabled=False), scope.engine)


@pytest.mark.parametrize("enabled", [False, True])
def test_api_composes_optional_governed_ingestion_context(scope, monkeypatch, enabled):
    """Exercise real API startup and SQL jobs without unrelated inference/search services."""

    @contextmanager
    def without_inference(settings, store, telemetry):
        """Only external query dependencies are replaced; ingestion context and SQL stay real."""
        yield SimpleNamespace(
            catalog=SqlEvidenceCatalog(store.engine, settings.governed_catalog_id)
        )

    def current_admin():
        """Resolve the current test grant at each request; no demo identity is substituted."""
        return GrantStore(scope.engine).resolve(scope.actor.subject)

    monkeypatch.setattr("creditlens.api.open_workflow", without_inference)
    app = create_app(config(scope, ingestion_enabled=enabled))
    app.dependency_overrides[current_principal] = current_admin
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/admin/documents",
            json=source(scope).model_dump(mode="json"),
            headers={"Idempotency-Key": "api"},
        )
        assert response.status_code == (202 if enabled else 503)
        assert (app.state.jobs is not None) == enabled
    assert count_jobs(scope) == (1 if enabled else 0)


def test_cli_stages_governed_source_and_disabled_mode_has_no_write(scope, tmp_path):
    """Staging uses the governed queue; disabled mode rejects before filesystem writes."""
    data = b"%PDF-1.4\nSynthetic staging bytes; parsing is separately tested."
    spec = source(scope).model_copy(update={"source_sha256": sha256(data).hexdigest()})
    pdf, manifest = tmp_path / "input.pdf", tmp_path / "manifest.json"
    pdf.write_bytes(data)
    manifest.write_text(spec.model_dump_json(), encoding="utf-8")
    args = argparse.Namespace(
        command="submit",
        queue_endpoint=None,
        queue_url=None,
        source_root=tmp_path / "sources",
        manifest=manifest,
        pdf=pdf,
        subject=scope.actor.subject,
        key="cli",
    )
    with pytest.raises(ValueError, match="Enable"):
        execute(args, config(scope, ingestion_enabled=False))
    assert not args.source_root.exists()
    result = json.loads(execute(args, config(scope)))
    assert result["state"] == "QUEUED"
    assert count_jobs(scope) == 1
    with scope.engine.connect() as connection:
        assert (
            connection.scalar(
                select(queue_bindings.c.catalog_id).where(queue_bindings.c.queue_id == scope.queue)
            )
            == scope.catalog.catalog_id
        )
