"""Persistent single-job worker with paced retries, SQL recovery and graceful stopping."""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from threading import Event

from sqlalchemy.exc import SQLAlchemyError

from creditlens.errors import ServiceError
from creditlens.ingestion_worker import IngestionWorker
from creditlens.queue_worker import QueueResult, QueueWorker


def emit_event(event: dict[str, object]) -> None:
    """Flush curated events so supervision never requires private exception text."""
    print(json.dumps(event), flush=True)


class WorkerLoop:
    """Reuse clients and circuit state while keeping one bounded extraction in flight."""

    def __init__(
        self,
        worker: IngestionWorker,
        queue_worker: QueueWorker | None = None,
        *,
        interval: float = 1,
        emit: Callable[[dict[str, object]], None] = emit_event,
    ) -> None:
        """Pace all outcomes, including poison messages and outages, to avoid a busy retry loop."""
        if not 0.1 <= interval <= 30:
            raise ValueError("Worker interval must be 0.1..30 seconds")
        self.worker, self.queue_worker, self.interval, self.emit = (
            worker,
            queue_worker,
            interval,
            emit,
        )

    def _iteration(self, stopping: Callable[[], bool]) -> QueueResult:
        """A broker outage cannot strand durable SQL jobs; database errors still propagate."""
        if self.queue_worker:
            try:
                return self.queue_worker.run_one(wait_seconds=1, stop_requested=stopping)
            except ServiceError as error:
                if error.code not in {"queue_unavailable", "queue_circuit_open"}:
                    raise
                self.emit({"event": "broker_unavailable", "error_code": error.code})
        if stopping():
            return QueueResult(disposition="STOPPING")
        result = self.worker.run_one()
        return QueueResult(disposition="RECOVERED" if result else "IDLE", result=result)

    def run(
        self,
        stop: Event,
        *,
        stop_file: Path | None = None,
        max_iterations: int | None = None,
    ) -> int:
        """Stop after this iteration; leave an unprocessed received message unacknowledged."""
        if max_iterations is not None and not 1 <= max_iterations <= 10_000:
            raise ValueError("Iteration limit must be 1..10000")

        def stopping() -> bool:
            """Only the trusted operator configures the stop path; queue data cannot select it."""
            return stop.is_set() or (stop_file is not None and stop_file.exists())

        completed = 0
        self.emit({"event": "worker_started"})
        while not stopping() and (max_iterations is None or completed < max_iterations):
            try:
                result = self._iteration(stopping)
                self.emit({"event": "iteration", **result.model_dump(mode="json")})
            except (ServiceError, SQLAlchemyError, subprocess.SubprocessError, OSError) as error:
                code = error.code if isinstance(error, ServiceError) else "worker_unavailable"
                self.emit({"event": "iteration_failed", "error_code": code})
            completed += 1
            if max_iterations is None or completed < max_iterations:
                stop.wait(self.interval)
        self.emit({"event": "worker_stopped", "iterations": completed})
        return completed
