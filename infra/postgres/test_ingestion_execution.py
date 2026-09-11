"""Real PostgreSQL and isolated Docker execution, with explicit boundary fault injection."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from sqlalchemy.engine import make_url

from creditlens.corpus import _write_pdf, borrower_pages
from creditlens.domain import Principal
from creditlens.errors import ServiceError
from creditlens.ingestion_jobs import IngestionInput, JobStore, initialize_jobs, jobs
from creditlens.ingestion_worker import DockerPdfExtractor, IngestionWorker, WorkerFailure
from creditlens.pdf_worker import ParsedDocument, extract_document
from creditlens.source_store import LocalSourceStore
from creditlens.sql_catalog import initialize_catalog
from creditlens.storage import grants, open_database


@pytest.fixture
def execution():
    """Use unique authority and actor records in an explicitly disposable local database."""
    url = os.environ.get("CREDITLENS_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Actual PostgreSQL URL is required")
    parsed = make_url(url)
    if parsed.host not in {"localhost", "127.0.0.1"} or parsed.database != "creditlens_test":
        raise ValueError("Execution tests require loopback creditlens_test")
    engine = open_database(url)
    temporary = TemporaryDirectory(prefix="creditlens-execution-")
    tmp_path = Path(temporary.name)
    initialize_jobs(engine)
    actor = Principal(
        subject=uuid4().hex,
        tenant_id="demo-bank",
        role="admin",
        borrower_ids=("borrower-001",),
        acl_groups=("underwriting",),
        revision=1,
    )
    with engine.begin() as connection:
        connection.execute(grants.insert().values(**actor.model_dump(mode="json"), enabled=True))
    store = JobStore(engine, "synthetic-execution-" + uuid4().hex)
    catalog = initialize_catalog(engine, store.queue_id)
    sources = LocalSourceStore(tmp_path / "sources")
    pdf = tmp_path / "original.pdf"
    pages = borrower_pages(1)[:2]
    _write_pdf(pdf, pages)
    digest = sources.stage(actor.tenant_id, pdf.read_bytes())
    source = IngestionInput(source_sha256=digest, pages=pages, parser="digital")
    try:
        yield store, catalog, sources, actor, source
    finally:
        engine.dispose()
        temporary.cleanup()


@pytest.fixture
def image():
    """Never pull an image or mistake an absent Docker check for a passing execution test."""
    value = os.environ.get("CREDITLENS_TEST_PARSER_IMAGE")
    if not value:
        pytest.skip("Set CREDITLENS_TEST_PARSER_IMAGE to a locally built immutable image ID")
    return value


class LocalExtractor:
    """Use real PDF parsing for database failure tests; this is not isolation evidence."""

    def __init__(self, sources):
        """Resolve only staged fixture bytes in the trusted local test source store."""
        self.sources = sources

    def extract(self, data, source, heartbeat):
        """Keep actual parsing while injecting faults around durable parent transitions."""
        heartbeat()
        return extract_document(
            self.sources.path(source.pages[0].tenant_id, source.source_sha256), source
        )


def test_real_container_publishes_actual_source_and_cleans_up(execution, image) -> None:
    """Exercise staging, leases, child JSON and atomic publication without parser mocks."""
    store, catalog, sources, actor, source = execution
    extractor = DockerPdfExtractor(image)
    worker = IngestionWorker(store, sources, catalog, extractor)
    job = store.submit(source, actor, "actual-container")
    result = worker.run_one(job.job_id)
    assert result.state == "COMPLETED"
    assert store.status(job.job_id, actor).state == "COMPLETED"
    chunks, _ = catalog.snapshot(actor, "borrower-001", source.pages[0].valid_from)
    assert len(chunks) == 2
    assert any("operating_cash_flow=180000.00" in chunk.text for chunk in chunks)
    assert all("pending extraction" not in chunk.text for chunk in chunks)
    assert worker.run_one(job.job_id) is None


def test_real_container_rejects_malformed_pdf(execution, image) -> None:
    """A valid source hash and PDF header do not establish structurally readable evidence."""
    store, catalog, sources, actor, source = execution
    digest = sources.stage(actor.tenant_id, b"%PDF-1.7\nprivate malformed document")
    source = source.model_copy(update={"source_sha256": digest})
    job = store.submit(source, actor, "malformed")
    result = IngestionWorker(store, sources, catalog, DockerPdfExtractor(image)).run_one(job.job_id)
    assert (result.state, result.error_code) == ("FAILED", "extraction_failed")
    assert not catalog.snapshot(actor, "borrower-001", source.pages[0].valid_from)[0]


def test_timeout_removes_real_isolated_container(execution, image) -> None:
    """Inspect running isolation and force a stalled child past its deadline, then check removal."""
    _, _, sources, actor, source = execution

    class StalledExtractor(DockerPdfExtractor):
        """Only substitute the parser program; all production isolation arguments stay intact."""

        def _command(self, name, directory):
            """Sleep in the owned container to reproduce a hung parser deterministically."""
            self.owned_name = name
            command = super()._command(name, directory)
            return command[:-2] + ["-c", "import time; time.sleep(30)"]

    extractor = StalledExtractor(image, timeout_seconds=4)
    inspected = []

    def inspect_isolation():
        """Inspect only the owned container after startup completes."""
        result = subprocess.run(  # noqa: S603 - trusted executable and owned container name.
            [extractor.executable, "inspect", extractor.owned_name],
            capture_output=True,
            check=False,
            timeout=5,
        )
        if result.returncode or inspected:
            return
        state = json.loads(result.stdout)[0]
        config = state["HostConfig"]
        assert config["NetworkMode"] == "none" and config["ReadonlyRootfs"]
        assert config["Memory"] == 512 * 1024 * 1024
        assert config["PidsLimit"] == 64 and config["NanoCpus"] == 1_000_000_000
        assert state["Config"]["User"] == "1000:1000"
        assert config["CapDrop"] == ["ALL"]
        assert len(state["Mounts"]) == 1 and not state["Mounts"][0]["RW"]
        inspected.append(True)

    with pytest.raises(WorkerFailure, match="worker_timeout"):
        extractor.extract(
            sources.read(actor.tenant_id, source.source_sha256), source, inspect_isolation
        )
    assert inspected
    removed = subprocess.run(  # noqa: S603 - trusted executable and owned container name.
        [extractor.executable, "inspect", extractor.owned_name],
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert removed.returncode != 0


@pytest.mark.parametrize("fault", ["missing", "tampered", "wrong_hash", "wrong_scope"])
def test_source_and_publication_faults_cannot_admit_pages(execution, monkeypatch, fault) -> None:
    """Inject source and child-output faults around real durable job and catalog transactions."""
    store, catalog, sources, actor, source = execution
    extractor = LocalExtractor(sources)
    job = store.submit(source, actor, fault)
    path = sources.path(actor.tenant_id, source.source_sha256)
    expected = ("FAILED", "invalid_source")
    if fault == "missing":
        path.unlink()
        expected = ("RETRY", "source_unavailable")
    elif fault == "tampered":
        path.write_bytes(b"%PDF-1.7\nchanged")
    else:
        original = extractor.extract

        def changed_output(data, spec, heartbeat):
            """Alter a real extraction result to exercise the parent output validation boundary."""
            result = original(data, spec, heartbeat)
            if fault == "wrong_hash":
                return result.model_copy(update={"source_sha256": "f" * 64})
            return ParsedDocument(
                source_sha256=result.source_sha256,
                pages=tuple(p.model_copy(update={"tenant_id": "other"}) for p in result.pages),
            )

        monkeypatch.setattr(extractor, "extract", changed_output)
        if fault == "wrong_scope":
            expected = ("FAILED", "publication_failed")
    result = IngestionWorker(store, sources, catalog, extractor).run_one(job.job_id)
    assert (result.state, result.error_code) == expected
    assert not catalog.snapshot(actor, "borrower-001", source.pages[0].valid_from)[0]


@pytest.mark.parametrize("fault", ["revoked", "expired", "database", "invalid_json", "timeout"])
def test_mid_parse_authority_and_execution_failures(execution, monkeypatch, fault) -> None:
    """Change database authority during parsing; uncertain failures cannot acknowledge work."""
    store, catalog, sources, actor, source = execution
    extractor = LocalExtractor(sources)
    job = store.submit(source, actor, fault)

    def fail_during_parse(data, spec, heartbeat):
        """Fault injection retains real heartbeat validation and durable failure transitions."""
        if fault == "invalid_json":
            raise ValueError("untrusted child JSON")
        if fault == "timeout":
            raise WorkerFailure("worker_timeout", retryable=True)
        if fault == "database":
            raise ServiceError("ingestion_unavailable", "unavailable", 503)
        with store.engine.begin() as connection:
            if fault == "revoked":
                connection.execute(
                    grants.update().where(grants.c.subject == actor.subject).values(enabled=False)
                )
            else:
                connection.execute(
                    jobs.update()
                    .where(jobs.c.job_id == job.job_id)
                    .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
                )
        heartbeat()
        raise AssertionError("Authority change must stop parsing")

    monkeypatch.setattr(extractor, "extract", fail_during_parse)
    worker = IngestionWorker(store, sources, catalog, extractor)
    if fault == "database":
        with pytest.raises(ServiceError, match="ingestion_unavailable"):
            worker.run_one(job.job_id)
        assert store.status(job.job_id, actor).state == "RUNNING"
    else:
        expected = {
            "revoked": ("FAILED", "permission_changed"),
            "expired": ("LEASE_LOST", "lease_lost"),
            "invalid_json": ("FAILED", "extraction_failed"),
            "timeout": ("RETRY", "worker_timeout"),
        }
        result = worker.run_one(job.job_id)
        assert (result.state, result.error_code) == expected[fault]
    assert not catalog.snapshot(actor, "borrower-001", source.pages[0].valid_from)[0]


def test_unsupported_ocr_stays_queued_and_missing_source_exhausts_retries(execution) -> None:
    """Parser routing consumes no OCR attempts, while durable source retries stop after three."""
    store, catalog, sources, actor, source = execution
    worker = IngestionWorker(store, sources, catalog, LocalExtractor(sources))
    ocr = store.submit(source.model_copy(update={"parser": "ocr"}), actor, "ocr")
    assert worker.run_one(ocr.job_id) is None
    assert store.status(ocr.job_id, actor).attempts == 0
    sources.path(actor.tenant_id, source.source_sha256).unlink()
    job = store.submit(source, actor, "missing")
    for attempt in range(1, 4):
        result = worker.run_one(job.job_id)
        assert result.state == ("RETRY" if attempt < 3 else "FAILED")
        with store.engine.begin() as connection:
            connection.execute(
                jobs.update()
                .where(jobs.c.job_id == job.job_id)
                .values(available_at=datetime.now(UTC) - timedelta(seconds=1))
            )
    assert result.error_code == "attempts_exhausted"
    assert worker.run_one(job.job_id) is None


def test_extractor_requires_bounded_operator_configuration(image) -> None:
    """Reject mutable tags, unbounded deadlines and ambiguous mount paths before launching."""
    with pytest.raises(ValueError, match="pinned"):
        DockerPdfExtractor("creditlens-pdf-parser:latest")
    for deadline in (0, 121, float("nan")):
        with pytest.raises(ValueError, match="deadline"):
            DockerPdfExtractor(image, timeout_seconds=deadline)
    with pytest.raises(ValueError, match="comma"):
        DockerPdfExtractor(image)._command("owned-test", Path("path,with-comma"))


def test_absent_image_is_retryable_without_pulling(execution) -> None:
    """A missing local artifact must not download an image or become an invalid-source failure."""
    store, catalog, sources, actor, source = execution
    job = store.submit(source, actor, "missing-image")
    result = IngestionWorker(
        store, sources, catalog, DockerPdfExtractor("sha256:" + "0" * 64)
    ).run_one(job.job_id)
    assert (result.state, result.error_code) == ("RETRY", "worker_unavailable")


@pytest.mark.parametrize(
    "program",
    [
        "raise SystemExit(124)",
        "import sys,time; sys.stderr.write('x'*1100000); sys.stderr.flush(); time.sleep(10)",
    ],
)
def test_child_deadline_status_and_log_overflow_are_curated(execution, image, program) -> None:
    """Actual child output crosses parser supervision, preserving timeout and size failures."""
    _, _, sources, actor, source = execution

    class FailingExtractor(DockerPdfExtractor):
        """Replace only the inner program to force specific child boundary failures."""

        def _command(self, name, directory):
            """Keep production isolation and cleanup while running a controlled fault program."""
            return super()._command(name, directory)[:-2] + ["-c", program]

    expected = "worker_timeout" if "SystemExit" in program else "extraction_failed"
    with pytest.raises(WorkerFailure, match=expected):
        FailingExtractor(image).extract(
            sources.read(actor.tenant_id, source.source_sha256), source, lambda: None
        )


def test_operator_cli_stages_and_executes_real_job(execution, image) -> None:
    """Separate Python invocations must retain job state and process the staged PDF in Docker."""
    store, catalog, sources, actor, source = execution
    root = sources.root.parent
    manifest = root / "manifest.json"
    manifest.write_text(source.model_dump_json(), encoding="utf-8")
    environment = {k: v for k, v in os.environ.items() if not k.startswith("CREDITLENS_")}
    environment.update(
        {
            "CREDITLENS_MODE": "demo",
            "CREDITLENS_DATABASE_URL": os.environ["CREDITLENS_TEST_POSTGRES_URL"],
            "CREDITLENS_CATALOG_BACKEND": "postgres",
            "CREDITLENS_DEMO_CATALOG_ID": store.queue_id,
            "CREDITLENS_INGESTION_ENABLED": "true",
            "CREDITLENS_INGESTION_QUEUE_ID": store.queue_id,
        }
    )
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[2] / "scripts/ingest_documents.py"),
        "--source-root",
        str(root / "operator-sources"),
    ]

    def invoke(arguments, expected=0):
        """Exercise the actual CLI; capture output privately and require curated JSON only."""
        result = subprocess.run(  # noqa: S603 - fixed local Python and explicitly owned fixture paths.
            command + arguments,
            env=environment,
            capture_output=True,
            timeout=75,
            check=False,
        )
        assert result.returncode == expected, result.stderr.decode()
        return json.loads(result.stdout)

    staging = [
        "submit",
        "--pdf",
        str(root / "original.pdf"),
        "--manifest",
        str(manifest),
        "--subject",
        actor.subject,
        "--key",
        "operator-cli",
    ]
    job = invoke(staging)
    assert job["state"] == "QUEUED"
    assert invoke(staging)["job_id"] == job["job_id"]
    processing = ["work-one", "--image", image, "--job-id", job["job_id"]]
    assert invoke(processing)["state"] == "COMPLETED"
    assert invoke(processing) == {"state": "IDLE"}
    chunks, _ = catalog.snapshot(actor, "borrower-001", source.pages[0].valid_from)
    assert any("operating_cash_flow=180000.00" in chunk.text for chunk in chunks)
    with store.engine.begin() as connection:
        connection.execute(
            grants.update().where(grants.c.subject == actor.subject).values(enabled=False)
        )
    assert invoke(staging, expected=2)["error_code"] == "access_denied"
    manifest.write_bytes(b"x" * 1_000_001)
    assert invoke(staging, expected=2) == {"error_code": "operator_input_invalid"}
    environment["CREDITLENS_DATABASE_URL"] = environment["CREDITLENS_DATABASE_URL"].replace(
        "/creditlens_test", "/creditlens_missing_" + uuid4().hex
    )
    assert invoke(processing, expected=2) == {"error_code": "ingestion_unavailable"}
