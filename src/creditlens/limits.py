"""Bound HTTP input before JSON parsing and limit repeat work per current identity."""

from collections import OrderedDict
from hashlib import sha256
from threading import Lock
from time import monotonic
from typing import TYPE_CHECKING, cast

import anyio
from redis.exceptions import RedisError
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from creditlens.cache import RedisBytes
from creditlens.errors import ServiceError

if TYPE_CHECKING:
    from creditlens.settings import Settings


class BodyLimit:
    """Count streamed bytes instead of trusting a caller-supplied Content-Length header."""

    def __init__(self, app: ASGIApp, max_bytes: int = 16384) -> None:
        """Keep the bound above the valid query contract while rejecting oversized JSON early."""
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Buffer only bounded query bodies and reject slow or oversized requests before routing."""
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return
        try:
            body = await self.read_body(receive)
        except ServiceError as error:
            response = JSONResponse(
                {"error": {"code": error.code, "message": error.message}}, status_code=error.status
            )
            await response(scope, receive, send)
            return
        delivered = False

        async def bounded_receive() -> Message:
            """Replay exactly one verified body, then preserve downstream disconnect behavior."""
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, bounded_receive, send)

    async def read_body(self, receive: Receive) -> bytes:
        """A finite wall-clock budget also limits clients that send no final body fragment."""
        body = bytearray()
        try:
            with anyio.fail_after(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        raise ServiceError("request_disconnected", "Request disconnected", 400)
                    fragment = message.get("body", b"")
                    if len(body) + len(fragment) > self.max_bytes:
                        raise ServiceError(
                            "request_too_large", "Request body exceeds the size limit", 413
                        )
                    body.extend(fragment)
                    if not message.get("more_body", False):
                        return bytes(body)
        except TimeoutError as error:
            raise ServiceError("request_timeout", "Request body timed out", 408) from error


class PrivateResponses:
    """Prevent browser or proxy caching independently of the frontend fetch configuration."""

    def __init__(self, app: ASGIApp) -> None:
        """Cover every API success and error response with one transport-level invariant."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Mutate only response headers, leaving streaming bodies and status codes intact."""

        async def private_send(message: Message) -> None:
            """Keep evidence and authorization errors out of shared intermediary caches."""
            if message["type"] == "http.response.start" and scope.get("path", "").startswith(
                "/api/"
            ):
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"cache-control"
                ]
                message["headers"] = headers + [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                ]
            await send(message)

        await self.app(scope, receive, private_send)


class QueryLimiter:
    """A bounded per-process demo limiter complements shared gateway limits in deployment."""

    def __init__(self, limit: int = 60, window_seconds: float = 60) -> None:
        """Bound identity cardinality so arbitrary requests cannot grow limiter memory forever."""
        self.limit = limit
        self.window_seconds = window_seconds
        self._entries: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._lock = Lock()

    def check(self, subject: str) -> None:
        """Limit authenticated subjects while resolving permissions independently per request."""
        now = monotonic()
        with self._lock:
            start, count = self._entries.get(subject, (now, 0))
            if now - start >= self.window_seconds:
                start, count = now, 0
            if count >= self.limit:
                raise ServiceError("rate_limited", "Request limit reached; retry later", 429)
            self._entries[subject] = (start, count + 1)
            self._entries.move_to_end(subject)
            if len(self._entries) > 10000:
                self._entries.popitem(last=False)

    def close(self) -> None:
        """Match shared limiter lifecycle without allocating transport resources for demos."""


QUOTA_SCRIPT = """
local value = redis.call('GET', KEYS[1])
if value then
    local count = tonumber(value)
    if not count or count < 1 or count % 1 ~= 0 or redis.call('PTTL', KEYS[1]) < 1 then
        return -1
    end
    if count >= tonumber(ARGV[1]) then return 0 end
    redis.call('INCR', KEYS[1])
else
    redis.call('SET', KEYS[1], 1, 'PX', ARGV[2])
end
return 1
"""


class RedisQueryLimiter(QueryLimiter):
    """Share expiring per-identity counters, with deployment memory bounded by noeviction."""

    def __init__(self, url: str, namespace: str, limit: int, window_seconds: int) -> None:
        """Reuse bounded TLS transport, but fail closed rather than bypassing a quota outage."""
        self.backend = RedisBytes(url)
        self.namespace = namespace
        self.limit = limit
        self.window_seconds = window_seconds

    def key(self, subject: str) -> str:
        """Opaque identity keys expire; only operators configure the namespace."""
        digest = sha256(subject.encode()).hexdigest()
        return f"creditlens:quota:v1:{self.namespace}:{digest}"

    def check(self, subject: str) -> None:
        """One atomic EVAL includes expiration; no retry can double-count an ambiguous write."""
        try:
            result = cast(
                int,
                self.backend.client.eval(
                    QUOTA_SCRIPT, 1, self.key(subject), self.limit, self.window_seconds * 1000
                ),
            )
        except RedisError as error:
            raise ServiceError(
                "quota_unavailable", "Request quota temporarily unavailable", 503
            ) from error
        if result == 0:
            raise ServiceError("rate_limited", "Request limit reached; retry later", 429)
        if result != 1:
            raise ServiceError("quota_unavailable", "Request quota temporarily unavailable", 503)

    def close(self) -> None:
        """Release only the application-owned Redis connection pool during shutdown."""
        self.backend.close()


def create_query_limiter(config: "Settings") -> QueryLimiter:
    """Use the same explicit shared quota policy across HTTP and local RPC entry points."""
    if config.quota_redis_url.get_secret_value():
        return RedisQueryLimiter(
            config.quota_redis_url.get_secret_value(),
            config.quota_namespace,
            config.query_limit,
            config.query_window_seconds,
        )
    return QueryLimiter(config.query_limit, config.query_window_seconds)
