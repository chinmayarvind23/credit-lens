"""Expose the existing evidence workflow through a bounded, versioned internal RPC contract."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import grpc
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from creditlens.auth import Authenticator
from creditlens.domain import QueryRequest
from creditlens.errors import ServiceError
from creditlens.limits import QueryLimiter
from creditlens.workflow import QueryWorkflow
from infra.rpc import creditlens_pb2, creditlens_pb2_grpc

STATUS = {
    400: grpc.StatusCode.INVALID_ARGUMENT,
    401: grpc.StatusCode.UNAUTHENTICATED,
    403: grpc.StatusCode.PERMISSION_DENIED,
    404: grpc.StatusCode.NOT_FOUND,
    408: grpc.StatusCode.DEADLINE_EXCEEDED,
    409: grpc.StatusCode.ABORTED,
    422: grpc.StatusCode.INVALID_ARGUMENT,
    429: grpc.StatusCode.RESOURCE_EXHAUSTED,
    503: grpc.StatusCode.UNAVAILABLE,
}


class UnderwritingService(creditlens_pb2_grpc.UnderwritingServicer):
    """Accept identity tokens, never a caller-supplied tenant, ACL or grant snapshot."""

    def __init__(self, auth: Authenticator, workflow: QueryWorkflow, limiter: QueryLimiter):
        """Share the current authority and workflow rather than copying financial logic."""
        self.auth, self.workflow, self.limiter = auth, workflow, limiter

    def Query(self, request, context):
        """Validate the wire boundary before work and never expose uncategorized exception text."""
        try:
            return self._query(request, context)
        except ServiceError as error:
            context.abort(STATUS.get(error.status, grpc.StatusCode.INTERNAL), error.code)
        except ValidationError:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "invalid_request")
        except SQLAlchemyError:
            context.abort(grpc.StatusCode.UNAVAILABLE, "storage_unavailable")
        except Exception:
            context.abort(grpc.StatusCode.INTERNAL, "internal_error")

    def _query(self, request, context):
        """Use typed failures internally so abort exceptions cannot replace curated status codes."""
        remaining = context.time_remaining()
        if remaining is None or remaining > 30:
            raise ServiceError(
                "deadline_required", "A client deadline of at most 30 seconds is required", 400
            )
        if not context.is_active() or remaining <= 0:
            raise ServiceError("deadline_exceeded", "Request deadline elapsed", 408)
        headers = [m.value for m in context.invocation_metadata() if m.key == "authorization"]
        if len(headers) > 1 or any(not isinstance(v, str) for v in headers):
            raise ServiceError("unauthenticated", "One authorization value is required", 401)
        principal = self.auth.authenticate(headers[0] if headers else None)
        body = QueryRequest(
            borrower_id=request.borrower_id,
            question=request.question,
            effective_at=request.effective_at,
        )
        self.limiter.check(principal.subject)
        packet = self.workflow.query(body, principal)
        if not context.is_active():
            raise ServiceError("deadline_exceeded", "Request deadline elapsed", 408)
        content = packet.model_dump_json().encode()
        if len(content) > 1_000_000:
            raise ServiceError("response_too_large", "Response exceeds the RPC limit", 429)
        return creditlens_pb2.PacketResponse(schema_version=1, packet_json=content)


@contextmanager
def serve_local(service: UnderwritingService, port: int = 0) -> Iterator[str]:
    """Own a loopback listener only; remote deployment requires an explicitly authenticated host."""
    if not 0 <= port <= 65535:
        raise ValueError("Invalid loopback port")
    with ThreadPoolExecutor(max_workers=4) as workers:
        server = grpc.server(
            workers,
            maximum_concurrent_rpcs=8,
            options=[
                ("grpc.max_receive_message_length", 16384),
                ("grpc.max_send_message_length", 1_048_576),
            ],
        )
        creditlens_pb2_grpc.add_UnderwritingServicer_to_server(service, server)
        selected = server.add_insecure_port(f"127.0.0.1:{port}")
        if selected == 0:
            raise RuntimeError("Owned RPC listener could not bind")
        server.start()
        try:
            yield f"127.0.0.1:{selected}"
        finally:
            server.stop(grace=5).wait(timeout=10)
