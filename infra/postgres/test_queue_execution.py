"""Actual local SQS-compatible delivery combined with PostgreSQL and isolated PDF execution."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from test_ingestion_execution import LocalExtractor
from test_ingestion_execution import execution as execution
from test_ingestion_execution import image as image

from creditlens.errors import ServiceError
from creditlens.ingestion_worker import DockerPdfExtractor, IngestionWorker
from creditlens.queue_worker import QueueWorker
from creditlens.sqs_queue import Delivery, Notification, SqsQueue, local_client


@pytest.fixture
def queue():
    """Create and delete only uniquely named synthetic queues on the explicit local broker."""
    endpoint = os.environ.get("CREDITLENS_TEST_SQS_ENDPOINT")
    if not endpoint:
        pytest.skip("Set CREDITLENS_TEST_SQS_ENDPOINT for actual ElasticMQ checks")
    pytest.importorskip("boto3")
    client = local_client(endpoint)
    name = "creditlens-" + uuid4().hex
    dead = client.create_queue(QueueName=name + "-dead")["QueueUrl"]
    arn = client.get_queue_attributes(QueueUrl=dead, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    url = client.create_queue(
        QueueName=name,
        Attributes={
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": arn, "maxReceiveCount": 3})
        },
    )["QueueUrl"]
    adapter = SqsQueue(endpoint, url)
    adapter.test_dead_queue = dead
    try:
        yield adapter
    finally:
        adapter.close()
        client.delete_queue(QueueUrl=url)
        client.delete_queue(QueueUrl=dead)
        client.close()


def test_actual_send_visibility_redelivery_and_delete(queue) -> None:
    """The same message reappears with a fresh receipt; only the latest receipt acknowledges it."""
    job_id = str(uuid4())
    queue.send(job_id)
    first = queue.receive(wait_seconds=0)
    assert str(Notification.model_validate_json(first.body).job_id) == job_id
    assert queue.receive(wait_seconds=0) is None
    queue.defer(first, seconds=0)
    again = queue.receive(wait_seconds=0)
    assert again.body == first.body and again.receipt != first.receipt
    queue.delete(again)
    assert queue.receive(wait_seconds=0) is None


def test_shared_client_sends_concurrent_notifications_without_loss(queue) -> None:
    """API background tasks can share the SDK pool and synchronized circuit state."""
    identifiers = {str(uuid4()) for _ in range(8)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(queue.send, identifiers))
    received = set()
    for _ in identifiers:
        delivery = queue.receive(wait_seconds=0)
        received.add(str(Notification.model_validate_json(delivery.body).job_id))
        queue.delete(delivery)
    assert received == identifiers


def test_duplicate_delivery_and_lost_notification_publish_once(queue, execution, image) -> None:
    """Real broker, SQL and parser checks prove duplicates leave the catalog epoch unchanged."""
    store, catalog, sources, actor, source = execution
    consumer = QueueWorker(
        queue, IngestionWorker(store, sources, catalog, DockerPdfExtractor(image))
    )
    job = store.submit(source, actor, "duplicate")
    queue.send(job.job_id)
    queue.send(job.job_id)
    assert consumer.run_one(wait_seconds=0).disposition == "ACKNOWLEDGED"
    chunks, revision = catalog.snapshot(actor, "borrower-001", source.pages[0].valid_from)
    assert any("operating_cash_flow=180000.00" in c.text for c in chunks)
    assert consumer.run_one(wait_seconds=0).disposition == "ACKNOWLEDGED"
    assert catalog.snapshot(actor, "borrower-001", source.pages[0].valid_from)[1] == revision
    assert store.status(job.job_id, actor).attempts == 1
    # An intentionally omitted notification must still recover from the authoritative SQL queue.
    second = store.submit(source, actor, "lost-notification")
    recovered = consumer.run_one(wait_seconds=0)
    assert recovered.disposition == "RECOVERED" and recovered.result.state == "COMPLETED"
    assert store.status(second.job_id, actor).state == "COMPLETED"
    assert catalog.snapshot(actor, "borrower-001", source.pages[0].valid_from)[1] == revision
    assert consumer.run_one(wait_seconds=0).disposition == "IDLE"


def test_bad_notifications_reach_actual_dead_letter_queue(queue, execution, monkeypatch) -> None:
    """Malformed messages never become SQL jobs and are retained by the broker's redrive policy."""
    store, catalog, sources, _, _ = execution
    consumer = QueueWorker(queue, IngestionWorker(store, sources, catalog, LocalExtractor(sources)))
    queue.client.send_message(QueueUrl=queue.queue_url, MessageBody='{"path":"private.pdf"}')
    original = queue.receive
    deliveries = []

    def observe_delivery(*, wait_seconds):
        """Retain the real receipt to accelerate visibility while preserving broker responses."""
        delivery = original(wait_seconds=wait_seconds)
        if delivery:
            deliveries.append(delivery)
        return delivery

    monkeypatch.setattr(queue, "receive", observe_delivery)
    for _ in range(3):
        assert consumer.run_one(wait_seconds=0).disposition == "INVALID"
        queue.defer(deliveries[-1], seconds=0)
    assert consumer.run_one(wait_seconds=0).disposition == "IDLE"
    dead = queue.client.receive_message(QueueUrl=queue.test_dead_queue, WaitTimeSeconds=1)
    assert dead["Messages"][0]["Body"] == '{"path":"private.pdf"}'
    assert store.claim() is None


def test_pending_unknown_and_unsupported_jobs_are_not_acknowledged(queue, execution) -> None:
    """A message cannot bypass another lease, invent a job or route OCR into digital parsing."""
    store, catalog, sources, actor, source = execution
    consumer = QueueWorker(queue, IngestionWorker(store, sources, catalog, LocalExtractor(sources)))
    job = store.submit(source, actor, "active")
    assert store.claim(job.job_id)
    queue.send(job.job_id)
    assert consumer.run_one(wait_seconds=0).disposition == "PENDING"
    queue.send(str(uuid4()))
    assert consumer.run_one(wait_seconds=0).disposition == "UNKNOWN"
    ocr = store.submit(source.model_copy(update={"parser": "ocr"}), actor, "ocr")
    queue.send(ocr.job_id)
    assert consumer.run_one(wait_seconds=0).disposition == "UNKNOWN"
    assert store.status(ocr.job_id, actor).attempts == 0


def test_delete_failure_preserves_terminal_job_for_redelivery(
    queue, execution, monkeypatch
) -> None:
    """A broker failure after SQL commit must retry notification deletion, never publication."""
    from botocore.exceptions import EndpointConnectionError

    store, catalog, sources, actor, source = execution
    worker = IngestionWorker(store, sources, catalog, LocalExtractor(sources))
    consumer = QueueWorker(queue, worker)
    job = store.submit(source, actor, "delete-outage")
    queue.send(job.job_id)
    original_receive, original_delete = queue.receive, queue.client.delete_message
    delivered = []

    def receive(*, wait_seconds):
        """Retain the receipt to release visibility after injected transport failure."""
        delivery = original_receive(wait_seconds=wait_seconds)
        delivered.append(delivery)
        return delivery

    def unavailable(**kwargs):
        """Inject only deletion transport; the database and preceding parser work remain real."""
        raise EndpointConnectionError(endpoint_url="private endpoint")

    monkeypatch.setattr(queue, "receive", receive)
    monkeypatch.setattr(queue.client, "delete_message", unavailable)
    with pytest.raises(ServiceError, match="queue_unavailable"):
        consumer.run_one(wait_seconds=0)
    assert store.status(job.job_id, actor).state == "COMPLETED"
    _, revision = catalog.snapshot(actor, "borrower-001", source.pages[0].valid_from)
    monkeypatch.setattr(queue.client, "delete_message", original_delete)
    queue.defer(delivered[-1], seconds=0)
    assert consumer.run_one(wait_seconds=0).disposition == "ACKNOWLEDGED"
    assert store.status(job.job_id, actor).attempts == 1
    assert catalog.snapshot(actor, "borrower-001", source.pages[0].valid_from)[1] == revision


def test_actual_broker_error_opens_and_recovers_circuit(queue) -> None:
    """Real missing-queue responses open the breaker; a later successful probe resets it."""
    moment = [100.0]
    queue.clock = lambda: moment[0]
    queue.client.delete_queue(QueueUrl=queue.queue_url)
    for _ in range(3):
        with pytest.raises(ServiceError, match="queue_unavailable"):
            queue.send(str(uuid4()))
    with pytest.raises(ServiceError, match="queue_circuit_open"):
        queue.receive(wait_seconds=0)
    queue.client.create_queue(QueueName=queue.queue_url.rsplit("/", 1)[1])
    moment[0] += 31
    queue.send(str(uuid4()))
    assert queue.failures == 0
    delivery = queue.receive(wait_seconds=0)
    assert delivery is not None
    queue.delete(delivery)


def test_queue_configuration_rejects_external_origins_and_unsafe_bounds(queue, monkeypatch) -> None:
    """Reject invalid endpoints and prevent environment overrides from redirecting local tests."""
    for endpoint in (
        "https://sqs.us-east-1.amazonaws.com",
        "http://localhost:19324",
        "http://127.0.0.1:0",
        "http://user:secret@127.0.0.1:19324",
        "http://127.0.0.1:19324/path",
        "http://127.0.0.1:19324?override=x",
        "http://127.0.0.1:19324#fragment",
    ):
        with pytest.raises(ValueError):
            SqsQueue(endpoint, queue.queue_url)
    endpoint = os.environ["CREDITLENS_TEST_SQS_ENDPOINT"]
    for url in (
        "https://example.com/queue",
        endpoint + "/000000000000/creditlens-../other",
        endpoint + "/000000000000/other",
        queue.queue_url + "?extra=1",
    ):
        with pytest.raises(ValueError):
            SqsQueue(endpoint, url)
    for value in (-1, 21):
        with pytest.raises(ValueError):
            queue.receive(wait_seconds=value)
    with pytest.raises(ValueError):
        queue.send("not-a-uuid")
    for value in (-1, 181):
        with pytest.raises(ValueError):
            queue.defer(Delivery(body="", receipt="synthetic"), seconds=value)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://example.com")
    client = local_client(endpoint)
    try:
        assert client.meta.endpoint_url == endpoint
        assert client.meta.config.proxies == {}
        assert client._request_signer._credentials.access_key == "creditlens-local-only"
        client.send_message(QueueUrl=queue.queue_url, MessageBody='{"test":"local"}')
        assert queue.receive(wait_seconds=0).body == '{"test":"local"}'
    finally:
        client.close()


@pytest.mark.parametrize(
    "messages",
    [
        [{"Body": "{}"}],
        [{"ReceiptHandle": "receipt"}],
        [{"Body": "x" * 2049, "ReceiptHandle": "receipt"}],
        [{"Body": "{}", "ReceiptHandle": "one"}, {"Body": "{}", "ReceiptHandle": "two"}],
    ],
)
def test_sdk_response_contract_rejects_missing_and_oversize_fields(queue, messages) -> None:
    """SDK-level response injection exercises malformed broker boundaries, not live semantics."""
    from botocore.stub import Stubber

    with Stubber(queue.client) as stub:
        stub.add_response("receive_message", {"Messages": messages})
        with pytest.raises(ServiceError, match="invalid_notification"):
            queue.receive(wait_seconds=0)


def test_operator_notification_and_recovery_commands(queue, execution, image, monkeypatch) -> None:
    """Separate CLI processes send notifications and recover committed jobs after send failure."""
    store, _, sources, actor, source = execution
    root = sources.root.parent
    manifest = root / "queue-manifest.json"
    manifest.write_text(source.model_dump_json(), encoding="utf-8")
    for name, value in {
        "DATABASE_URL": os.environ["CREDITLENS_TEST_POSTGRES_URL"],
        "MODE": "demo",
        "CATALOG_BACKEND": "postgres",
        "DEMO_CATALOG_ID": store.queue_id,
        "INGESTION_ENABLED": "true",
        "INGESTION_QUEUE_ID": store.queue_id,
    }.items():
        monkeypatch.setenv("CREDITLENS_" + name, value)
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[2] / "scripts/ingest_documents.py"),
        "--source-root",
        str(sources.root),
        "--queue-endpoint",
        os.environ["CREDITLENS_TEST_SQS_ENDPOINT"],
        "--queue-url",
        queue.queue_url,
    ]

    def invoke(arguments):
        """Use owned paths and literal argv, then require actual successful JSON output."""
        result = subprocess.run(  # noqa: S603 - fixed local Python and owned fixture arguments.
            command + arguments,
            capture_output=True,
            timeout=75,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode()
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
        "notified",
    ]
    first = invoke(staging)
    assert first["notification"] == "SENT"
    work = ["work-queue", "--image", image, "--wait", "0"]
    assert invoke(work)["disposition"] == "ACKNOWLEDGED"
    assert store.status(first["job_id"], actor).state == "COMPLETED"
    # A valid local URL for a missing queue reproduces send failure without network mocking.
    command[-1] = queue.queue_url + "-missing"
    staging[-1] = "missed"
    second = invoke(staging)
    assert second["state"] == "QUEUED" and second["notification"] == "PENDING"
    assert store.status(second["job_id"], actor).state == "QUEUED"
    command[-1] = queue.queue_url
    assert invoke(work)["disposition"] == "RECOVERED"
    assert store.status(second["job_id"], actor).state == "COMPLETED"
