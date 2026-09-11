"""Actual API, broker, database and worker-process checks for continuous ingestion."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_ingestion_execution import LocalExtractor
from test_ingestion_execution import execution as execution
from test_ingestion_execution import image as image
from test_queue_execution import queue as queue

from creditlens.api import create_app, current_principal
from creditlens.errors import ServiceError
from creditlens.ingestion_worker import IngestionWorker
from creditlens.queue_worker import QueueWorker
from creditlens.settings import Settings
from creditlens.storage import GrantStore
from creditlens.worker_loop import WorkerLoop


def configuration(execution, queue, *, missing=False):
    """Bind API and worker to the same synthetic authority without production identities."""
    store, _, _, _, _ = execution
    return Settings(
        database_url=os.environ["CREDITLENS_TEST_POSTGRES_URL"],
        catalog_backend="postgres",
        demo_catalog_id=store.queue_id,
        ingestion_enabled=True,
        ingestion_queue_id=store.queue_id,
        ingestion_sqs_endpoint=os.environ["CREDITLENS_TEST_SQS_ENDPOINT"],
        ingestion_sqs_queue_url=queue.queue_url + ("-missing" if missing else ""),
    )


def authorized_app(config, execution):
    """Replace only Cognito transport while retaining current SQL grant resolution per request."""
    store, _, _, actor, _ = execution
    app = create_app(config)

    def current_admin():
        """Read actual grant authority rather than retaining the submitted role snapshot."""
        return GrantStore(store.engine).resolve(actor.subject)

    app.dependency_overrides[current_principal] = current_admin
    return app


def test_background_notification_failure_preserves_accepted_job(queue, execution, caplog) -> None:
    """A real missing-queue response cannot undo HTTP acceptance; the loop recovers SQL intent."""
    store, _, _, actor, source = execution
    with TestClient(
        authorized_app(configuration(execution, queue, missing=True), execution)
    ) as api:
        response = api.post(
            "/api/v1/admin/documents",
            json=source.model_dump(mode="json"),
            headers={"Idempotency-Key": "missing-notification"},
        )
        assert response.status_code == 202
        assert store.status(response.json()["job_id"], actor).state == "QUEUED"
        assert "queue_unavailable" in caplog.text
        assert source.source_sha256 not in caplog.text
        assert queue.receive(wait_seconds=0) is None


def test_loop_recovers_sql_during_broker_outage_and_continues_after_storage_error(
    queue,
    execution,
    monkeypatch,
) -> None:
    """Bound injected transport/storage failures while retaining actual SQL and PDF processing."""
    store, catalog, sources, actor, source = execution
    worker = IngestionWorker(store, sources, catalog, LocalExtractor(sources))
    job = store.submit(source, actor, "outage")
    consumer = QueueWorker(queue, worker)
    events = []
    original = worker.run_one
    calls = []

    def broker_unavailable(**kwargs):
        """Replace the broker boundary only; fallback must use the real durable worker."""
        raise ServiceError("queue_unavailable", "Queue unavailable")

    def first_storage_failure(job_id=None):
        """Fail one claim before any mutation, then restore real SQL processing on the next poll."""
        calls.append(True)
        if len(calls) == 1:
            raise ServiceError("ingestion_unavailable", "Storage unavailable")
        return original(job_id)

    monkeypatch.setattr(queue, "receive", broker_unavailable)
    monkeypatch.setattr(worker, "run_one", first_storage_failure)
    assert (
        WorkerLoop(worker, consumer, interval=0.1, emit=events.append).run(
            Event(), max_iterations=2
        )
        == 2
    )
    assert any(e.get("error_code") == "ingestion_unavailable" for e in events)
    assert store.status(job.job_id, actor).state == "COMPLETED"


def test_stop_during_poll_leaves_notification_and_job_unacknowledged(queue, execution) -> None:
    """Receiving a message after shutdown intent cannot start a new database claim."""
    store, catalog, sources, actor, source = execution
    job = store.submit(source, actor, "stop-after-poll")
    queue.send(job.job_id)
    worker = IngestionWorker(store, sources, catalog, LocalExtractor(sources))
    assert (
        QueueWorker(queue, worker).run_one(wait_seconds=0, stop_requested=lambda: True).disposition
        == "STOPPING"
    )
    assert store.status(job.job_id, actor).attempts == 0
    assert queue.receive(wait_seconds=0) is None


def test_loop_stop_and_parameter_boundaries(execution, monkeypatch) -> None:
    """Reject busy-loop settings and honor preexisting stop intent."""
    store, catalog, sources, _, _ = execution
    worker = IngestionWorker(store, sources, catalog, LocalExtractor(sources))
    for interval in (0, 31):
        with pytest.raises(ValueError):
            WorkerLoop(worker, interval=interval)
    loop = WorkerLoop(worker, interval=0.1)
    with pytest.raises(ValueError):
        loop.run(Event(), max_iterations=0)
    stop = Event()
    stop.set()
    assert loop.run(stop) == 0
    assert loop._iteration(lambda: True).disposition == "STOPPING"
    assert loop.run(Event(), max_iterations=1) == 1


def test_continuous_worker_process_handles_api_submission_and_stop_file(queue, execution, image):
    """A child process ingests an API job, exposes its source and stops through an operator file."""
    store, catalog, sources, actor, source = execution
    # The API seeds the demo catalog; new ingestion must use its own immutable document identity.
    identity = "ingested-" + uuid4().hex
    source = source.model_copy(
        update={
            "pages": tuple(p.model_copy(update={"document_id": identity}) for p in source.pages)
        }
    )
    config = configuration(execution, queue)
    environment = dict(os.environ)
    for name in (
        "database_url",
        "catalog_backend",
        "demo_catalog_id",
        "ingestion_enabled",
        "ingestion_queue_id",
        "ingestion_sqs_endpoint",
        "ingestion_sqs_queue_url",
    ):
        environment["CREDITLENS_" + name.upper()] = str(getattr(config, name))
    root = sources.root.parent
    stop_file, output, errors = root / "STOP", root / "daemon.jsonl", root / "daemon-errors.log"
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[2] / "scripts/ingest_documents.py"),
        "--source-root",
        str(sources.root),
        "work-loop",
        "--image",
        image,
        "--interval",
        "0.1",
        "--stop-file",
        str(stop_file),
        "--max-iterations",
        "100",
    ]
    with (
        TestClient(authorized_app(config, execution)) as api,
        output.open("wb") as stdout,
        errors.open("wb") as stderr,
    ):
        process = subprocess.Popen(  # noqa: S603 - fixed local Python and owned fixture paths.
            command,
            env=environment,
            stdout=stdout,
            stderr=stderr,
        )
        try:
            deadline = time.monotonic() + 45
            while "worker_started" not in output.read_text():
                assert process.poll() is None, errors.read_text()
                assert time.monotonic() < deadline
                time.sleep(0.1)
            response = api.post(
                "/api/v1/admin/documents",
                json=source.model_dump(mode="json"),
                headers={"Idempotency-Key": "continuous-api"},
            )
            assert response.status_code == 202
            job_id = response.json()["job_id"]
            while store.status(job_id, actor).state not in {"COMPLETED", "FAILED"}:
                assert process.poll() is None, errors.read_text()
                assert time.monotonic() < deadline
                time.sleep(0.1)
            assert api.get(f"/api/v1/admin/index-jobs/{job_id}").json()["state"] == "COMPLETED"
            chunks, _ = catalog.snapshot(actor, "borrower-001", source.pages[0].valid_from)
            published = next(c for c in chunks if c.document_id == identity)
            page = api.get(
                f"/api/v1/evidence/{published.chunk_id}",
                params={
                    "borrower_id": "borrower-001",
                    "effective_at": source.pages[0].valid_from.isoformat(),
                },
            )
            assert page.status_code == 200 and page.json()["text"] == published.text
            stop_file.touch()
            assert process.wait(timeout=10) == 0, errors.read_text()
            events = [json.loads(line) for line in output.read_text().splitlines()]
            assert any(e.get("event") == "worker_stopped" for e in events)
            assert events[-1]["state"] == "STOPPED"
            print(json.dumps({"verified_daemon_events": events, "source_document": identity}))
        finally:
            stop_file.touch()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_partial_disabled_or_external_api_queue_configuration_is_rejected(queue, execution):
    """Validate settings before SDK initialization to prevent accidental cloud routing."""
    with pytest.raises(ValueError):
        Settings(ingestion_sqs_endpoint="http://127.0.0.1:19324")
    config = configuration(execution, queue)
    for changed in (
        {"ingestion_enabled": False},
        {"ingestion_sqs_endpoint": "https://sqs.us-east-1.amazonaws.com"},
    ):
        with pytest.raises(ValueError):
            Settings(**{**config.model_dump(), **changed})


def test_loop_retains_job_after_invalid_broker_response(queue, execution, monkeypatch) -> None:
    """Invalid broker data must not silently trigger publication or acknowledge the delivery."""
    store, catalog, sources, actor, source = execution
    job = store.submit(source, actor, "invalid-response")
    worker = IngestionWorker(store, sources, catalog, LocalExtractor(sources))

    def invalid(**kwargs):
        """Inject the response-validation error already covered by SDK response contracts."""
        raise ServiceError("invalid_notification", "Invalid notification")

    monkeypatch.setattr(queue, "receive", invalid)
    events = []
    WorkerLoop(worker, QueueWorker(queue, worker), emit=events.append).run(
        Event(), max_iterations=1
    )
    assert any(e.get("error_code") == "invalid_notification" for e in events)
    assert store.status(job.job_id, actor).attempts == 0
