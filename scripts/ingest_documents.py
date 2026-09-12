"""Trusted local operator entry point for staging PDFs and processing durable digital jobs."""

import argparse
import json
import signal
import subprocess
from hashlib import sha256
from pathlib import Path
from threading import Event

from sqlalchemy.exc import SQLAlchemyError

from creditlens.errors import ServiceError
from creditlens.ingestion_jobs import IngestionInput, JobStatus, JobStore, _authorize
from creditlens.ingestion_worker import DockerPdfExtractor, IngestionWorker
from creditlens.ocr import OcrDocument, OcrReview
from creditlens.queue_worker import QueueWorker
from creditlens.settings import Settings
from creditlens.source_store import LocalSourceStore
from creditlens.sql_catalog import SqlEvidenceCatalog
from creditlens.sqs_queue import SqsQueue
from creditlens.storage import GrantStore, open_database
from creditlens.worker_loop import WorkerLoop


def bounded_read(path: Path, limit: int) -> bytes:
    """Bound operator input before JSON/PDF validation; file paths never come from queue data."""
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Input exceeds its configured byte limit")
    return data


def submit(args: argparse.Namespace, store: JobStore, sources: LocalSourceStore) -> str:
    """Require current explicit admin scope before persisting bytes or submitting intent."""
    source = IngestionInput.model_validate_json(bounded_read(args.manifest, 1_000_000))
    actor = GrantStore(store.engine).resolve(args.subject)
    _authorize(actor, source)
    data = bounded_read(args.pdf, 25_000_000)
    if sha256(data).hexdigest() != source.source_sha256:
        raise ValueError("Source hash does not match the submitted PDF")
    sources.stage(actor.tenant_id, data)
    # Resolve again after file I/O; the worker also checks grants through publication.
    actor = GrantStore(store.engine).resolve(args.subject)
    return store.submit(source, actor, args.key).model_dump_json()


def handle_ocr(
    args: argparse.Namespace, store: JobStore, sources: LocalSourceStore, settings: Settings
) -> str:
    """Trusted offline artifacts enter quarantine only after source and current grant checks."""
    actor = GrantStore(store.engine).resolve(args.subject)
    store.status(args.job_id, actor)
    if args.command == "show-ocr":
        artifact = store.review_artifact(args.job_id, actor)
        return json.dumps(
            {"artifact_sha256": artifact.digest(), "artifact": artifact.model_dump(mode="json")}
        )
    data = bounded_read(args.input, 8_000_000)
    if args.command == "review-ocr":
        return store.review_ocr(
            args.job_id,
            actor,
            OcrReview.model_validate_json(data),
            SqlEvidenceCatalog(store.engine, settings.demo_catalog_id),
        ).model_dump_json()
    artifact = OcrDocument.model_validate_json(data)
    lease = store.claim(args.job_id, parser="ocr")
    if lease is None:
        raise ServiceError("review_unavailable", "OCR job cannot be claimed", 409)
    try:
        sources.read(lease.input.pages[0].tenant_id, lease.input.source_sha256)
        store.stage_ocr(lease, artifact)
    except (OSError, ValueError):
        store.fail(lease, "invalid_source", retryable=False)
        raise
    return store.status(args.job_id, actor).model_dump_json()


def notify_submission(status: str, queue: SqsQueue | None) -> str:
    """A failed notification does not undo a committed job; expose the need for SQL recovery."""
    if queue is None:
        return status
    job = JobStatus.model_validate_json(status)
    notification = "SENT"
    try:
        queue.send(job.job_id)
    except ServiceError:
        notification = "PENDING"
    return json.dumps({**job.model_dump(mode="json"), "notification": notification})


def execute(args: argparse.Namespace, settings: Settings) -> str:
    """Use the existing configured schema; this tool neither seeds identities nor creates grants."""
    if not settings.ingestion_enabled or settings.catalog_backend != "postgres":
        raise ValueError("Enable the configured PostgreSQL ingestion path first")
    endpoint = args.queue_endpoint or settings.ingestion_sqs_endpoint
    queue_url = args.queue_url or settings.ingestion_sqs_queue_url
    if bool(endpoint) != bool(queue_url):
        raise ValueError("Queue endpoint and URL must be configured together")
    engine = open_database(settings.database_url)
    queue = None
    try:
        if endpoint:
            queue = SqsQueue(endpoint, queue_url)
        store = JobStore(engine, settings.ingestion_queue_id)
        sources = LocalSourceStore(args.source_root)
        if args.command == "submit":
            return notify_submission(submit(args, store, sources), queue)
        if args.command in {"stage-ocr", "review-ocr", "show-ocr"}:
            return handle_ocr(args, store, sources, settings)
        worker = IngestionWorker(
            store,
            sources,
            SqlEvidenceCatalog(engine, settings.demo_catalog_id),
            DockerPdfExtractor(args.image, timeout_seconds=args.timeout),
        )
        if args.command == "work-loop":
            return run_loop(args, worker, queue)
        if args.command == "work-queue":
            if queue is None:
                raise ValueError("Queue processing requires an explicit local queue")
            return QueueWorker(queue, worker).run_one(wait_seconds=args.wait).model_dump_json()
        result = worker.run_one(args.job_id)
        return result.model_dump_json() if result else json.dumps({"state": "IDLE"})
    finally:
        if queue:
            queue.close()
        engine.dispose()


def run_loop(args: argparse.Namespace, worker: IngestionWorker, queue: SqsQueue | None) -> str:
    """Handle operator shutdown without killing a parser or acknowledging incomplete work."""
    stop = Event()

    def request_stop(signum: int, frame: object) -> None:
        """Mark stop intent; normal cleanup owns clients, leases and child processes."""
        stop.set()

    previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        count = WorkerLoop(
            worker, QueueWorker(queue, worker) if queue else None, interval=args.interval
        ).run(stop, stop_file=args.stop_file, max_iterations=args.max_iterations)
        return json.dumps({"state": "STOPPED", "iterations": count})
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main() -> None:
    """Process one job per invocation; the operator controls scheduling and retained sources."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--queue-endpoint")
    parser.add_argument("--queue-url")
    commands = parser.add_subparsers(dest="command", required=True)
    staging = commands.add_parser("submit")
    staging.add_argument("--pdf", type=Path, required=True)
    staging.add_argument("--manifest", type=Path, required=True)
    staging.add_argument("--subject", required=True)
    staging.add_argument("--key", required=True)
    for name in ("stage-ocr", "review-ocr", "show-ocr"):
        review = commands.add_parser(name)
        review.add_argument("--job-id", required=True)
        review.add_argument("--subject", required=True)
        if name != "show-ocr":
            review.add_argument("--input", type=Path, required=True)
    for command in ("work-one", "work-queue", "work-loop"):
        worker = commands.add_parser(command)
        worker.add_argument("--image", required=True)
        worker.add_argument("--timeout", type=float, default=60)
        if command == "work-one":
            worker.add_argument("--job-id")
        elif command == "work-queue":
            worker.add_argument("--wait", type=int, default=10)
        else:
            worker.add_argument("--interval", type=float, default=1)
            worker.add_argument("--stop-file", type=Path)
            worker.add_argument("--max-iterations", type=int)
    args = parser.parse_args()
    try:
        print(execute(args, Settings()))
    except ServiceError as error:
        print(json.dumps({"error_code": error.code}))
        raise SystemExit(2) from None
    except SQLAlchemyError:
        print(json.dumps({"error_code": "ingestion_unavailable"}))
        raise SystemExit(2) from None
    except subprocess.SubprocessError:
        print(json.dumps({"error_code": "worker_unavailable"}))
        raise SystemExit(2) from None
    except (OSError, ValueError):
        # Validation strings and filesystem errors can contain private paths or document text.
        print(json.dumps({"error_code": "operator_input_invalid"}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
