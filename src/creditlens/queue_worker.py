"""Acknowledge broker delivery only after durable SQL state proves work is terminal."""

from collections.abc import Callable
from typing import Literal

from creditlens.domain import StrictModel
from creditlens.ingestion_worker import IngestionWorker, WorkerResult
from creditlens.sqs_queue import Notification, SqsQueue


class QueueResult(StrictModel):
    """Report delivery disposition without exposing message bodies or receipt handles."""

    disposition: Literal[
        "ACKNOWLEDGED", "RECOVERED", "IDLE", "INVALID", "PENDING", "UNKNOWN", "STOPPING"
    ]
    result: WorkerResult | None = None


class QueueWorker:
    """SQL leases and atomic publication remain correct across duplicate or lost notifications."""

    def __init__(self, queue: SqsQueue, worker: IngestionWorker) -> None:
        """Keep transport separate from source authority and parser execution."""
        self.queue, self.worker = queue, worker

    def run_one(
        self, *, wait_seconds: int = 10, stop_requested: Callable[[], bool] | None = None
    ) -> QueueResult:
        """Poll SQL when the broker is empty; never acknowledge unknown or malformed work."""
        delivery = self.queue.receive(wait_seconds=wait_seconds)
        if stop_requested and stop_requested():
            return QueueResult(disposition="STOPPING")
        if delivery is None:
            result = self.worker.run_one()
            return QueueResult(disposition="RECOVERED" if result else "IDLE", result=result)
        try:
            job_id = str(Notification.model_validate_json(delivery.body).job_id)
        except ValueError:
            return QueueResult(disposition="INVALID")
        result = self.worker.run_one(job_id)
        if result and result.state in {"COMPLETED", "FAILED", "REVIEW_REQUIRED"}:
            self.queue.delete(delivery)
            return QueueResult(disposition="ACKNOWLEDGED", result=result)
        disposition = (
            self.worker.jobs.delivery_disposition(job_id, include_ocr=True)
            if self.worker.ocr_extractor is not None
            else self.worker.jobs.delivery_disposition(job_id)
        )
        if disposition == "TERMINAL":
            self.queue.delete(delivery)
            return QueueResult(disposition="ACKNOWLEDGED", result=result)
        if disposition == "PENDING":
            self.queue.defer(delivery)
        return QueueResult(disposition=disposition, result=result)
