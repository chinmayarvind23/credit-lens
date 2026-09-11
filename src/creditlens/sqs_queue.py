"""SQS notifications carry job identity only; local execution cannot contact an AWS endpoint."""

import re
import time
from collections.abc import Callable
from threading import Lock
from typing import TYPE_CHECKING, TypeVar
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field

from creditlens.domain import StrictModel
from creditlens.errors import ServiceError

if TYPE_CHECKING:
    from mypy_boto3_sqs import SQSClient

T = TypeVar("T")


class Notification(StrictModel):
    """A message can identify existing work but cannot assert scope, source or completion."""

    job_id: UUID


class Delivery(StrictModel):
    """Receipt handles authorize deletion and must never appear in public status or logs."""

    body: str = Field(max_length=2048, repr=False)
    receipt: str = Field(min_length=1, max_length=4096, repr=False)


def local_endpoint(endpoint: str) -> str:
    """Use numeric loopback only, avoiding DNS, proxies, credentials and accidental AWS calls."""
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not parsed.port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Queue endpoint must be explicit numeric loopback HTTP")
    return endpoint.rstrip("/")


def local_client(endpoint: str) -> "SQSClient":
    """Load the optional official SDK only for deliberate local queue execution."""
    import boto3
    from botocore.config import Config

    # The installed stub omits a documented option supported by the pinned runtime.
    configuration = Config(  # type: ignore[call-arg]
        connect_timeout=2,
        read_timeout=25,
        proxies={},
        retries={"total_max_attempts": 2, "mode": "standard"},
        ignore_configured_endpoint_urls=True,
    )
    return boto3.client(
        "sqs",
        endpoint_url=local_endpoint(endpoint),
        region_name="us-east-1",
        aws_access_key_id="creditlens-local-only",
        aws_secret_access_key="creditlens-local-only",  # noqa: S106 - synthetic loopback credential.
        config=configuration,
    )


def queue_origin(endpoint: str, queue_url: str) -> str:
    """Validate configured destinations without importing the optional SDK or opening a client."""
    origin = local_endpoint(endpoint)
    parsed = urlsplit(queue_url)
    if (
        f"{parsed.scheme}://{parsed.netloc}" != origin
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/000000000000/creditlens-[A-Za-z0-9_-]{1,69}", parsed.path)
    ):
        raise ValueError("Queue URL must identify a local synthetic CreditLens queue")
    return origin


class SqsQueue:
    """Bound broker calls and stop repeated outages without confusing delivery with job state."""

    def __init__(
        self, endpoint: str, queue_url: str, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """Reject a queue on another origin before creating the client or reading credentials."""
        origin = queue_origin(endpoint, queue_url)
        self.client = local_client(origin)
        self.queue_url, self.clock = queue_url, clock
        self.failures, self.blocked_until = 0, 0.0
        self._lock = Lock()

    def _call(self, operation: Callable[[], T]) -> T:
        """Curate SDK failures; three failed operations open a 30-second process-local breaker."""
        from botocore.exceptions import BotoCoreError, ClientError

        with self._lock:
            if self.clock() < self.blocked_until:
                raise ServiceError("queue_circuit_open", "Queue is temporarily unavailable", 503)
        try:
            result = operation()
        except (BotoCoreError, ClientError) as error:
            with self._lock:
                self.failures += 1
                if self.failures >= 3:
                    self.blocked_until = self.clock() + 30
            raise ServiceError("queue_unavailable", "Queue is unavailable", 503) from error
        with self._lock:
            self.failures, self.blocked_until = 0, 0.0
        return result

    def send(self, job_id: str) -> None:
        """Only canonical UUID JSON is sent; a caller must authorize the existing SQL job first."""
        body = Notification(job_id=UUID(job_id)).model_dump_json()
        self._call(lambda: self.client.send_message(QueueUrl=self.queue_url, MessageBody=body))

    def receive(self, *, wait_seconds: int = 10) -> Delivery | None:
        """Receive one bounded notification with visibility longer than the digital parser limit."""
        if not 0 <= wait_seconds <= 20:
            raise ValueError("SQS wait must be 0..20 seconds")
        result = self._call(
            lambda: self.client.receive_message(
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=wait_seconds,
                VisibilityTimeout=180,
            )
        )
        messages = result.get("Messages", [])
        if not messages:
            return None
        if len(messages) != 1:
            raise ServiceError("invalid_notification", "Queue response is invalid", 503)
        try:
            return Delivery(body=messages[0]["Body"], receipt=messages[0]["ReceiptHandle"])
        except (KeyError, ValueError) as error:
            raise ServiceError("invalid_notification", "Queue response is invalid", 503) from error

    def delete(self, delivery: Delivery) -> None:
        """Delete only after durable terminal state, using the latest delivery's receipt handle."""
        self._call(
            lambda: self.client.delete_message(
                QueueUrl=self.queue_url,
                ReceiptHandle=delivery.receipt,
            )
        )

    def defer(self, delivery: Delivery, *, seconds: int = 30) -> None:
        """Retain delayed or active notifications; SQL still decides claim eligibility."""
        if not 0 <= seconds <= 180:
            raise ValueError("Notification defer must be 0..180 seconds")
        self._call(
            lambda: self.client.change_message_visibility(
                QueueUrl=self.queue_url,
                ReceiptHandle=delivery.receipt,
                VisibilityTimeout=seconds,
            )
        )

    def close(self) -> None:
        """Release the SDK connection pool when an operator invocation ends."""
        self.client.close()
