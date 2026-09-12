"""Actual PostgreSQL job tests exercise duplicate delivery, leases and durable failure state."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from creditlens.api import create_app, current_principal
from creditlens.corpus import borrower_pages
from creditlens.domain import Principal
from creditlens.errors import ServiceError
from creditlens.ingestion_jobs import IngestionInput, JobStore, initialize_jobs, jobs
from creditlens.settings import Settings
from creditlens.sql_catalog import initialize_catalog
from creditlens.storage import GrantStore, grants, open_database


@pytest.fixture
def store():
    """Require a disposable loopback database; never infer locks from an SQLite substitute."""
    url = os.environ.get("CREDITLENS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set CREDITLENS_TEST_POSTGRES_URL for actual PostgreSQL job tests")
    parsed = make_url(url)
    if parsed.host not in {"127.0.0.1", "localhost"} or parsed.database != "creditlens_test":
        raise ValueError("Job tests require the loopback creditlens_test database")
    engine = open_database(url)
    initialize_jobs(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(grants)
            .values(**admin().model_dump(mode="json"), enabled=True)
            .on_conflict_do_nothing()
        )
    try:
        yield JobStore(engine, "synthetic-test-" + uuid4().hex)
    finally:
        engine.dispose()


def admin() -> Principal:
    """Give the synthetic administrator one borrower and one ACL, not implicit universal access."""
    return Principal(
        subject="job-admin",
        tenant_id="demo-bank",
        role="admin",
        borrower_ids=("borrower-001",),
        acl_groups=("underwriting",),
        revision=1,
    )


def spec() -> IngestionInput:
    """Use metadata as scope only; extraction must replace its preexisting text."""
    return IngestionInput(source_sha256="a" * 64, pages=borrower_pages(1)[:2], parser="digital")


def expire(store: JobStore, job_id: str) -> None:
    """Advance persisted fixture deadlines instead of waiting minutes or changing worker clocks."""
    with store.engine.begin() as connection:
        connection.execute(
            update(jobs)
            .where(jobs.c.job_id == job_id)
            .values(
                lease_until=datetime.now(UTC) - timedelta(seconds=1),
                available_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )


def test_submit_is_idempotent_but_changed_source_conflicts(store: JobStore) -> None:
    """Duplicate SQS delivery and repeated HTTP submission must not create duplicate work."""
    first = store.submit(spec(), admin(), "request-one")
    again = store.submit(spec(), admin(), "request-one")
    assert first.job_id == again.job_id
    assert first.state == "QUEUED"
    with pytest.raises(ServiceError, match="idempotency_conflict"):
        store.submit(spec().model_copy(update={"source_sha256": "b" * 64}), admin(), "request-one")
    changed_text = spec().model_copy(
        update={
            "pages": tuple(
                page.model_copy(update={"text": "not evidence"}) for page in spec().pages
            )
        }
    )
    assert store.submit(changed_text, admin(), "request-one").job_id == first.job_id


def test_submission_and_status_enforce_current_scope(store: JobStore) -> None:
    """Admin role alone cannot authorize another tenant, borrower or restricted page."""
    for actor in (
        admin().model_copy(update={"role": "underwriter"}),
        admin().model_copy(update={"tenant_id": "other"}),
        admin().model_copy(update={"borrower_ids": ()}),
        admin().model_copy(update={"acl_groups": ()}),
    ):
        with pytest.raises(ServiceError):
            store.submit(spec(), actor, "not-allowed")
    job = store.submit(spec(), admin(), "allowed")
    with pytest.raises(ServiceError):
        store.status(job.job_id, admin().model_copy(update={"tenant_id": "other"}))
    with pytest.raises(ServiceError):
        store.status(str(uuid4()), admin())


def test_concurrent_workers_claim_once_and_restart_preserves_lease(store: JobStore) -> None:
    """Independent connections race for a row; the database decides ownership exactly once."""
    job = store.submit(spec(), admin(), "concurrent")
    with ThreadPoolExecutor(max_workers=4) as pool:
        leases = list(pool.map(lambda _: store.claim(job.job_id), range(4)))
    claimed = [lease for lease in leases if lease is not None]
    assert len(claimed) == 1
    assert claimed[0].attempt == 1
    assert all(page.text == "pending extraction" for page in claimed[0].input.pages)
    restarted = JobStore(store.engine, store.queue_id)
    assert restarted.claim(job.job_id) is None
    assert restarted.status(job.job_id, admin()).state == "RUNNING"


def test_expiry_reclaims_with_fresh_token_and_rejects_stale_worker(store: JobStore) -> None:
    """A resumed old worker cannot heartbeat or overwrite the replacement worker's result."""
    job = store.submit(spec(), admin(), "expiry")
    old = store.claim(job.job_id)
    assert old is not None
    expire(store, job.job_id)
    new = store.claim(job.job_id)
    assert new is not None and new.token != old.token and new.attempt == 2
    with pytest.raises(ServiceError):
        store.heartbeat(old)
    with pytest.raises(ServiceError):
        store.fail(old, "source_unavailable", retryable=True)
    store.heartbeat(new)
    assert store.status(job.job_id, admin()).attempts == 2


def test_retry_delay_and_exhaustion_survive_restart(store: JobStore) -> None:
    """Retries are delayed and bounded even if every worker restarts after a failure."""
    job = store.submit(spec(), admin(), "retries")
    for attempt in range(1, 4):
        lease = JobStore(store.engine, store.queue_id).claim(job.job_id)
        assert lease is not None and lease.attempt == attempt
        store.fail(lease, "source_unavailable", retryable=True)
        assert store.claim(job.job_id) is None
        expire(store, job.job_id)
    status = store.status(job.job_id, admin())
    assert status.state == "FAILED" and status.error_code == "attempts_exhausted"
    assert store.claim(job.job_id) is None


def test_terminal_failure_is_durable_and_does_not_retry(store: JobStore) -> None:
    """Malformed source data cannot be retried indefinitely or reported as completed evidence."""
    job = store.submit(spec(), admin(), "terminal")
    lease = store.claim(job.job_id)
    assert lease is not None
    store.fail(lease, "invalid_source", retryable=False)
    assert store.status(job.job_id, admin()).state == "FAILED"
    expire(store, job.job_id)
    assert store.claim(job.job_id) is None


def seed_current_admin(store: JobStore, actor: Principal) -> None:
    """Persist a unique current grant so publication cannot rely on a submitted role snapshot."""
    with store.engine.begin() as connection:
        connection.execute(grants.insert().values(**actor.model_dump(mode="json"), enabled=True))


def test_publication_commits_catalog_and_job_together(store: JobStore) -> None:
    """Completed means current authorized pages were durably published, not just extracted."""
    actor = admin().model_copy(update={"subject": uuid4().hex})
    seed_current_admin(store, actor)
    job = store.submit(spec(), actor, "publish")
    lease = store.claim(job.job_id)
    assert lease is not None
    catalog = initialize_catalog(store.engine, store.queue_id)
    store.complete(lease, catalog, spec().pages)
    assert store.status(job.job_id, actor).state == "COMPLETED"
    chunks, _ = catalog.snapshot(actor, "borrower-001", spec().pages[0].valid_from)
    assert chunks and "pending extraction" not in chunks[0].text
    assert store.claim(job.job_id) is None
    with pytest.raises(ServiceError):
        store.complete(lease, catalog, spec().pages)


def test_revocation_and_changed_manifest_prevent_publication(store: JobStore) -> None:
    """Neither an old admin grant nor altered metadata may be used to admit evidence."""
    actor = admin().model_copy(update={"subject": uuid4().hex})
    seed_current_admin(store, actor)
    job = store.submit(spec(), actor, "revocation")
    lease = store.claim(job.job_id)
    assert lease is not None
    catalog = initialize_catalog(store.engine, store.queue_id)
    changed = tuple(page.model_copy(update={"tenant_id": "other"}) for page in spec().pages)
    with pytest.raises(ValueError):
        store.complete(lease, catalog, changed)
    with store.engine.begin() as connection:
        connection.execute(
            grants.update().where(grants.c.subject == actor.subject).values(enabled=False)
        )
    with pytest.raises(ServiceError):
        store.complete(lease, catalog, spec().pages)
    assert not catalog.snapshot(actor, "borrower-001", spec().pages[0].valid_from)[0]
    assert store.status(job.job_id, actor).state == "RUNNING"


def test_failure_after_catalog_write_rolls_back_entire_publication(
    store: JobStore, monkeypatch
) -> None:
    """Fail after real inserts to prove the job/catalog transaction has no partial commit."""
    actor = admin().model_copy(update={"subject": uuid4().hex})
    seed_current_admin(store, actor)
    job = store.submit(spec(), actor, "rollback")
    lease = store.claim(job.job_id)
    assert lease is not None
    catalog = initialize_catalog(store.engine, store.queue_id)
    original = catalog.publish_in_transaction

    def fail_after_insert(connection, pages):
        """Fail after real database mutation rather than mocking away the publication path."""
        original(connection, pages)
        raise RuntimeError("injected after publication")

    monkeypatch.setattr(catalog, "publish_in_transaction", fail_after_insert)
    with pytest.raises(RuntimeError):
        store.complete(lease, catalog, spec().pages)
    assert not catalog.snapshot(actor, "borrower-001", spec().pages[0].valid_from)[0]
    assert store.status(job.job_id, actor).state == "RUNNING"


def test_ocr_results_require_review_and_cannot_be_completed_automatically(store: JobStore) -> None:
    """A successful OCR process does not authorize automatic catalog admission."""
    actor = admin().model_copy(update={"subject": uuid4().hex})
    seed_current_admin(store, actor)
    job = store.submit(spec().model_copy(update={"parser": "ocr"}), actor, "ocr")
    lease = store.claim(job.job_id)
    assert lease is not None
    catalog = initialize_catalog(store.engine, store.queue_id)
    with pytest.raises(ServiceError):
        store.complete(lease, catalog, spec().pages)
    store.require_review(lease, "c" * 64)
    assert store.status(job.job_id, actor).state == "REVIEW_REQUIRED"
    assert store.claim(job.job_id) is None
    assert not catalog.snapshot(actor, "borrower-001", spec().pages[0].valid_from)[0]


def test_expired_third_attempt_becomes_terminal_on_recovery(store: JobStore) -> None:
    """Repeated worker death cannot bypass retry exhaustion by avoiding the explicit fail call."""
    job = store.submit(spec(), admin(), "crashes")
    for attempt in range(1, 4):
        lease = store.claim()
        assert lease is not None and lease.attempt == attempt
        expire(store, job.job_id)
    assert store.claim() is None
    assert store.status(job.job_id, admin()).error_code == "attempts_exhausted"


def test_expiry_during_publication_rolls_back_inserted_pages(store: JobStore, monkeypatch) -> None:
    """A lease expiring after inserts must roll back the catalog and the success acknowledgment."""
    actor = admin().model_copy(update={"subject": uuid4().hex})
    seed_current_admin(store, actor)
    job = store.submit(spec(), actor, "mid-publication-expiry")
    lease = store.claim(job.job_id)
    assert lease is not None
    catalog = initialize_catalog(store.engine, store.queue_id)
    original = catalog.publish_in_transaction

    def expire_after_insert(connection, pages):
        """Move the persisted deadline inside the real transaction to exercise the final fence."""
        revision = original(connection, pages)
        connection.execute(
            jobs.update()
            .where(jobs.c.job_id == job.job_id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        return revision

    monkeypatch.setattr(catalog, "publish_in_transaction", expire_after_insert)
    with pytest.raises(ServiceError, match="lease_lost"):
        store.complete(lease, catalog, spec().pages)
    assert store.status(job.job_id, actor).state == "RUNNING"
    assert not catalog.snapshot(actor, "borrower-001", spec().pages[0].valid_from)[0]


def test_invalid_bounds_and_unready_evidence_are_rejected(store: JobStore) -> None:
    """Reject unsupported storage, abusive manifests and low-confidence admission at boundaries."""
    sqlite = create_engine("sqlite://")
    try:
        with pytest.raises(ValueError):
            initialize_jobs(sqlite)
        with pytest.raises(ValueError):
            JobStore(sqlite, "test")
    finally:
        sqlite.dispose()
    with pytest.raises(ValueError):
        JobStore(store.engine, "test", lease_seconds=1)
    with pytest.raises(ValueError):
        IngestionInput(
            source_sha256="a" * 64,
            parser="digital",
            pages=(spec().pages[0].model_copy(update={"text": "x" * 1_000_001}),),
        )
    with pytest.raises(ValueError):
        store.submit(spec(), admin(), "invalid/key")
    job = store.submit(spec(), admin(), "validation")
    lease = store.claim(job.job_id)
    assert lease is not None
    catalog = initialize_catalog(store.engine, store.queue_id)
    for batch in (spec().pages[:1], lease.input.pages):
        with pytest.raises(ValueError):
            store.complete(lease, catalog, batch)
    with pytest.raises(ValueError):
        store.require_review(lease, "invalid")
    with pytest.raises(ValueError):
        store.fail(lease, "raw private exception", retryable=False)
    with pytest.raises(ServiceError):
        store.status(job.job_id, admin().model_copy(update={"role": "underwriter"}))


def test_database_failure_is_curated_and_rolls_back(store: JobStore, monkeypatch) -> None:
    """A failed database connection cannot expose private SQL messages or claim successful work."""

    def unavailable():
        """Fail at the connection boundary while retaining the real public error mapping."""
        raise OperationalError("private sql", {}, Exception("private password"))

    monkeypatch.setattr(store.engine, "begin", unavailable)
    with pytest.raises(ServiceError) as error:
        store.claim()
    assert error.value.code == "ingestion_unavailable"
    assert "private" not in error.value.message


def test_current_grant_remains_locked_until_publication_commits(
    store: JobStore, monkeypatch
) -> None:
    """Prove a concurrent grant revocation cannot interleave between authorization and commit."""
    actor = admin().model_copy(update={"subject": uuid4().hex})
    seed_current_admin(store, actor)
    job = store.submit(spec(), actor, "grant-race")
    lease = store.claim(job.job_id)
    assert lease is not None
    catalog = initialize_catalog(store.engine, store.queue_id)
    entered, release = Event(), Event()
    original = catalog.publish_in_transaction

    def paused_publish(connection, pages):
        """Pause after the real authorization lock while a second connection tests exclusion."""
        entered.set()
        if not release.wait(5):
            raise RuntimeError("Fixture release timed out")
        return original(connection, pages)

    monkeypatch.setattr(catalog, "publish_in_transaction", paused_publish)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(store.complete, lease, catalog, spec().pages)
        try:
            assert entered.wait(5)
            with store.engine.begin() as connection:
                with pytest.raises(OperationalError) as error:
                    connection.execute(
                        select(grants)
                        .where(grants.c.subject == actor.subject)
                        .with_for_update(nowait=True)
                    )
                assert error.value.orig.sqlstate == "55P03"
        finally:
            release.set()
        future.result(timeout=5)
    assert store.status(job.job_id, actor).state == "COMPLETED"


def test_admin_http_submission_status_restart_and_revocation(store: JobStore) -> None:
    """Exercise two actual API instances and SQL grants; only JWT transport uses a test seam."""
    actor = admin().model_copy(update={"subject": uuid4().hex})
    seed_current_admin(store, actor)
    config = Settings(
        database_url=store.engine.url.render_as_string(hide_password=False),
        catalog_backend="postgres",
        demo_catalog_id="synthetic-" + uuid4().hex,
        ingestion_enabled=True,
        ingestion_queue_id="synthetic-" + uuid4().hex,
    )
    first_app, second_app = create_app(config), create_app(config)

    def current_admin() -> Principal:
        """Use fresh SQL grants on every endpoint call while isolating Cognito transport."""
        return GrantStore(store.engine).resolve(actor.subject)

    for app in (first_app, second_app):
        app.dependency_overrides[current_principal] = current_admin
    headers = {"Idempotency-Key": "api-submission"}
    with TestClient(first_app) as first, TestClient(second_app) as second:
        assert first.get("/ready").status_code == 200
        response = first.post(
            "/api/v1/admin/documents", json=spec().model_dump(mode="json"), headers=headers
        )
        assert response.status_code == 202
        result = response.json()
        assert result["state"] == "QUEUED"
        assert set(result) == {
            "job_id",
            "state",
            "attempts",
            "error_code",
            "created_at",
            "updated_at",
        }
        repeated = second.post(
            "/api/v1/admin/documents", json=spec().model_dump(mode="json"), headers=headers
        )
        assert repeated.status_code == 202 and repeated.json()["job_id"] == result["job_id"]
        status_path = "/api/v1/admin/index-jobs/" + result["job_id"]
        assert second.get(status_path).json() == result
        changed = spec().model_copy(update={"source_sha256": "b" * 64})
        assert (
            first.post(
                "/api/v1/admin/documents", json=changed.model_dump(mode="json"), headers=headers
            ).status_code
            == 409
        )
        assert (
            first.post("/api/v1/admin/documents", json=spec().model_dump(mode="json")).status_code
            == 422
        )
        assert first.get("/api/v1/admin/index-jobs/invalid").status_code == 422
        first_app.state.limiter.limit = 0
        limited = first.post(
            "/api/v1/admin/documents", json=spec().model_dump(mode="json"), headers=headers
        )
        assert limited.status_code == 429 and limited.headers["Retry-After"] == "60"
    with TestClient(create_app(config)) as public:
        assert public.get(status_path).status_code == 403
    restarted = create_app(config)
    restarted.dependency_overrides[current_principal] = current_admin
    with TestClient(restarted) as client:
        assert client.get(status_path).json() == result
        with store.engine.begin() as connection:
            connection.execute(
                grants.update().where(grants.c.subject == actor.subject).values(enabled=False)
            )
        assert client.get(status_path).status_code == 403
        assert (
            client.post(
                "/api/v1/admin/documents", json=spec().model_dump(mode="json"), headers=headers
            ).status_code
            == 403
        )


def test_ingestion_is_opt_in_and_readiness_checks_its_store(store: JobStore, monkeypatch) -> None:
    """Disabled ingestion is explicit, and losing the enabled job dependency fails readiness."""
    with pytest.raises(ValidationError):
        Settings(ingestion_enabled=True)
    config = Settings(database_url=store.engine.url.render_as_string(hide_password=False))
    app = create_app(config)
    app.dependency_overrides[current_principal] = admin
    with TestClient(app) as client:
        assert client.get("/api/v1/admin/index-jobs/" + str(uuid4())).status_code == 503
    enabled = create_app(
        config.model_copy(update={"catalog_backend": "postgres", "ingestion_enabled": True})
    )
    with TestClient(enabled) as client:

        def unavailable() -> None:
            """Isolate readiness behavior after the separate actual-SQL failure tests pass."""
            raise ServiceError("ingestion_unavailable", "Ingestion storage is unavailable", 503)

        monkeypatch.setattr(enabled.state.jobs, "check_ready", unavailable)
        assert client.get("/ready").status_code == 503


def ocr_fixture(store):
    """Normalize deterministic OCR-shaped output; this tests admission, not OCR recognition."""
    import json

    from creditlens.ocr import OcrDocument, normalize_vl

    actor = admin().model_copy(update={"subject": uuid4().hex})
    seed_current_admin(store, actor)
    source = spec().model_copy(update={"parser": "ocr"})
    artifact = OcrDocument(
        pages=tuple(
            normalize_vl(
                json.dumps(
                    {
                        "width": 100,
                        "height": 100,
                        "parsing_res_list": [
                            {
                                "block_label": "text",
                                "block_content": page.text,
                                "block_bbox": [0, 0, 100, 100],
                                "block_order": 1,
                            }
                        ],
                    }
                ).encode(),
                page,
                pdf_sha256=source.source_sha256,
                image_sha256="b" * 64,
                models_sha256="c" * 64,
                generation_complete=True,
            )
            for page in source.pages
        )
    )
    job = store.submit(source, actor, "ocr-review")
    lease = store.claim(job.job_id, parser="ocr")
    assert lease is not None
    catalog = initialize_catalog(store.engine, store.queue_id)
    return actor, artifact, job, lease, catalog


def test_reviewed_ocr_is_quarantined_then_published_atomically(store):
    """A reviewed batch becomes exact canonical evidence while retaining original extraction."""
    from creditlens.ocr import OcrReview

    actor, artifact, job, lease, catalog = ocr_fixture(store)
    store.stage_ocr(lease, artifact)
    assert not catalog.snapshot(actor, "borrower-001", spec().pages[0].valid_from)[0]
    assert store.review_artifact(job.job_id, actor) == artifact
    corrections = tuple(page.metadata.text + " Human verified." for page in artifact.pages)
    review = OcrReview(
        artifact_sha256=artifact.digest(),
        decision="approve",
        reason="Compared against physical pages",
        corrected_text=corrections,
    )
    status = store.review_ocr(job.job_id, actor, review, catalog)
    assert status.state == "COMPLETED"
    chunks, epoch = catalog.snapshot(actor, "borrower-001", spec().pages[0].valid_from)
    assert chunks and epoch > 0
    assert all(chunk.parser_version.endswith("-human-reviewed") for chunk in chunks)
    assert any("Human verified." in chunk.text for chunk in chunks)
    with store.engine.connect() as connection:
        result = connection.execute(
            select(jobs.c.result).where(jobs.c.job_id == job.job_id)
        ).scalar_one()
    assert result["artifact"] == artifact.model_dump(mode="json")
    assert result["review"]["subject"] == actor.subject
    with pytest.raises(ServiceError, match="review_unavailable"):
        store.review_ocr(job.job_id, actor, review, catalog)


@pytest.mark.parametrize("mutation", ["hash", "scope", "text", "expired", "digital"])
def test_ocr_staging_rejects_wrong_source_scope_text_and_lease(store, mutation):
    """Artifact validation and fencing must fail before any evidence is visible."""
    from creditlens.ocr import OcrDocument

    actor, artifact, job, lease, catalog = ocr_fixture(store)
    payload = artifact.model_dump()
    if mutation == "hash":
        payload["pages"][0]["pdf_sha256"] = "f" * 64
    elif mutation == "scope":
        payload["pages"][0]["metadata"]["acl_groups"] = ("restricted",)
    elif mutation == "text":
        payload["pages"][0]["metadata"]["text"] = "forged"
    elif mutation == "expired":
        expire(store, job.job_id)
    else:
        with store.engine.begin() as connection:
            source = lease.input.model_dump(mode="json") | {"parser": "digital"}
            connection.execute(update(jobs).where(jobs.c.job_id == job.job_id).values(input=source))
    with pytest.raises((ValueError, ServiceError)):
        store.stage_ocr(lease, OcrDocument.model_validate(payload))
    assert not catalog.snapshot(actor, "borrower-001", spec().pages[0].valid_from)[0]


@pytest.mark.parametrize("failure", ["wrong_hash", "revoked", "stale_grant", "corrections"])
def test_review_rechecks_snapshot_and_current_grants(store, failure):
    """Stale decisions and revoked reviewers cannot admit quarantined evidence."""
    from creditlens.ocr import OcrReview

    actor, artifact, job, lease, catalog = ocr_fixture(store)
    store.stage_ocr(lease, artifact)
    review = OcrReview(artifact_sha256=artifact.digest(), decision="approve", reason="Checked")
    if failure == "wrong_hash":
        review = review.model_copy(update={"artifact_sha256": "f" * 64})
    elif failure in {"revoked", "stale_grant"}:
        with store.engine.begin() as connection:
            connection.execute(
                update(grants)
                .where(grants.c.subject == actor.subject)
                .values(**({"enabled": False} if failure == "revoked" else {"revision": 2}))
            )
    else:
        review = review.model_copy(update={"corrected_text": ("one page only",)})
    with pytest.raises((ValueError, ServiceError)):
        store.review_ocr(job.job_id, actor, review, catalog)
    assert store.status(job.job_id, actor).state == "REVIEW_REQUIRED"
    assert not catalog.snapshot(actor, "borrower-001", spec().pages[0].valid_from)[0]


def test_review_rejection_and_rollback_leave_no_evidence(store, monkeypatch):
    """Failure after real SQL inserts rolls everything back; rejection remains durable."""
    from creditlens.ocr import OcrReview

    actor, artifact, job, lease, catalog = ocr_fixture(store)
    store.stage_ocr(lease, artifact)
    review = OcrReview(artifact_sha256=artifact.digest(), decision="approve", reason="Checked")
    original = catalog.publish_in_transaction

    def fail_after_insert(connection, pages):
        """Inject failure after the real database write to exercise transaction rollback."""
        original(connection, pages)
        raise RuntimeError("after insert")

    monkeypatch.setattr(catalog, "publish_in_transaction", fail_after_insert)
    with pytest.raises(RuntimeError):
        store.review_ocr(job.job_id, actor, review, catalog)
    assert store.status(job.job_id, actor).state == "REVIEW_REQUIRED"
    assert not catalog.snapshot(actor, "borrower-001", spec().pages[0].valid_from)[0]
    rejected = store.review_ocr(
        job.job_id, actor, review.model_copy(update={"decision": "reject"}), catalog
    )
    assert rejected.state == "FAILED" and rejected.error_code == "review_rejected"
    assert not catalog.snapshot(actor, "borrower-001", spec().pages[0].valid_from)[0]


def test_ocr_review_api_protects_artifact_and_exposes_approved_sources(store):
    """Exercise real HTTP handlers and PostgreSQL from private review to cited source lookup."""
    actor, artifact, job, lease, catalog = ocr_fixture(store)
    store.stage_ocr(lease, artifact)
    config = Settings(
        database_url=store.engine.url.render_as_string(hide_password=False),
        catalog_backend="postgres",
        ingestion_enabled=True,
        ingestion_queue_id=store.queue_id,
    )
    app = create_app(config)
    path = f"/api/v1/admin/index-jobs/{job.job_id}/review"
    with TestClient(app) as client:
        app.state.workflow.catalog = type(catalog)(app.state.jobs.engine, catalog.catalog_id)
        assert client.get(path).status_code == 403
        app.dependency_overrides[current_principal] = lambda: actor
        snapshot = client.get(path)
        assert snapshot.status_code == 200
        assert snapshot.headers["Cache-Control"] == "no-store"
        assert snapshot.json()["artifact_sha256"] == artifact.digest()
        decision = {
            "artifact_sha256": artifact.digest(),
            "decision": "approve",
            "reason": "Checked",
        }
        assert client.post(path, json=decision).json()["state"] == "COMPLETED"
        chunks, _ = catalog.snapshot(actor, "borrower-001", spec().pages[0].valid_from)
        source = client.get(
            "/api/v1/evidence/" + chunks[0].chunk_id,
            params={"borrower_id": "borrower-001", "effective_at": str(spec().pages[0].valid_from)},
        )
        assert source.status_code == 200
        assert source.json()["text"] == chunks[0].text
        assert source.json()["page"] == chunks[0].page
        assert client.post(path, json=decision).status_code == 409


def test_operator_ocr_handoff_verifies_pdf_and_retains_review(store):
    """Run the real operator commands with staged PDF bytes and durable normalized output."""
    import argparse
    import io
    import json
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from pypdf import PdfWriter

    from creditlens.ocr import OcrDocument
    from creditlens.source_store import LocalSourceStore
    from scripts.ingest_documents import handle_ocr

    actor, artifact, _, _, catalog = ocr_fixture(store)
    with TemporaryDirectory(prefix="creditlens-review-") as temporary:
        root = Path(temporary)
        sources = LocalSourceStore(root / "sources")
        writer = PdfWriter()
        for _ in artifact.pages:
            writer.add_blank_page(width=100, height=100)
        stream = io.BytesIO()
        writer.write(stream)
        digest = sources.stage(actor.tenant_id, stream.getvalue())
        artifact = OcrDocument(
            pages=tuple(page.model_copy(update={"pdf_sha256": digest}) for page in artifact.pages)
        )
        source = spec().model_copy(update={"parser": "ocr", "source_sha256": digest})
        job = store.submit(source, actor, "operator")
        artifact_path = root / "ocr.json"
        artifact_path.write_text(artifact.model_dump_json(), encoding="utf-8")
        args = argparse.Namespace(
            command="stage-ocr", subject=actor.subject, job_id=job.job_id, input=artifact_path
        )
        settings = Settings(demo_catalog_id=catalog.catalog_id)
        assert json.loads(handle_ocr(args, store, sources, settings))["state"] == "REVIEW_REQUIRED"
        args.command = "show-ocr"
        snapshot = json.loads(handle_ocr(args, store, sources, settings))
        assert snapshot["artifact_sha256"] == artifact.digest()
        decision = {
            "artifact_sha256": artifact.digest(),
            "decision": "approve",
            "reason": "Checked",
        }
        artifact_path.write_text(json.dumps(decision), encoding="utf-8")
        args.command = "review-ocr"
        assert json.loads(handle_ocr(args, store, sources, settings))["state"] == "COMPLETED"


def test_graphql_explorer_uses_real_scoped_job_store(store):
    """Read the actual durable job contract through GraphQL without exposing private fields."""
    pytest.importorskip("graphql")
    actor = admin().model_copy(update={"subject": uuid4().hex})
    seed_current_admin(store, actor)
    job = store.submit(spec(), actor, "graphql")
    config = Settings(
        database_url=store.engine.url.render_as_string(hide_password=False),
        catalog_backend="postgres",
        ingestion_enabled=True,
        ingestion_queue_id=store.queue_id,
        graphql_enabled=True,
    )
    app = create_app(config)
    app.dependency_overrides[current_principal] = lambda: actor
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/admin/graphql",
            json={
                "query": "query Job($id: ID!){ingestionJob(id:$id){"
                "jobId state attempts errorCode}}",
                "variables": {"id": job.job_id},
            },
        )
        assert response.status_code == 200
        assert response.json()["data"]["ingestionJob"] == {
            "jobId": job.job_id,
            "state": "QUEUED",
            "attempts": 0,
            "errorCode": None,
        }
        missing = client.post(
            "/api/v1/admin/graphql",
            json={
                "query": "query Job($id: ID!){viewer{subject} ingestionJob(id:$id){state}}",
                "variables": {"id": str(uuid4())},
            },
        )
        assert missing.status_code == 404 and "data" not in missing.json()
