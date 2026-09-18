"""Cancel one model HTTP exchange without closing the generator's shared client."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from math import isfinite
from ssl import SSLContext
from time import monotonic
from types import TracebackType
from typing import Any, Protocol

import anyio
import httpx
from anyio.from_thread import BlockingPortal, start_blocking_portal


class StreamingClient(Protocol):
    """Keep generator decoding independent of the production client's async ownership."""

    def stream(
        self,
        method: str,
        url: str,
        *,
        json: Any,
        headers: dict[str, str],
        timeout: float,
        follow_redirects: bool,
    ) -> AbstractContextManager[httpx.Response]:
        """Yield a response whose body and HTTP lifecycle remain owned by the caller."""
        ...


class GenerationClient:
    """Expose synchronous requests through one cancellable, privately owned event loop."""

    def __init__(
        self,
        *,
        trust_env: bool = False,
        follow_redirects: bool = False,
        verify: SSLContext | bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Preserve explicit routing and certificate verification for evidence-bearing requests."""
        if trust_env or follow_redirects or verify is False:
            raise ValueError("Generation requires verified TLS and explicit routing")
        self._verify, self._transport = verify, transport
        self._stack: ExitStack | None = None
        self._portal: BlockingPortal | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def is_closed(self) -> bool:
        """Expose lifecycle state without leaking transport internals or borrower data."""
        return self._client is None or self._client.is_closed

    def __enter__(self) -> "GenerationClient":
        """Create the client on its portal and unwind startup failures in ownership order."""
        if self._stack is not None:
            raise RuntimeError("Generation client is already open")
        with ExitStack() as stack:
            portal = stack.enter_context(start_blocking_portal())
            client = httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                verify=self._verify,
                transport=self._transport,
            )
            self._client = stack.enter_context(portal.wrap_async_context_manager(client))
            self._portal, self._stack = portal, stack.pop_all()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close network resources before joining the dedicated loop thread on every exit."""
        self.close()

    def close(self) -> None:
        """Make normal and exceptional cleanup idempotent without request-level watchdogs."""
        stack, self._stack = self._stack, None
        try:
            if stack is not None:
                stack.close()
        finally:
            self._client, self._portal = None, None

    @contextmanager
    def stream(
        self,
        method: str,
        url: str,
        *,
        json: Any,
        headers: dict[str, str],
        timeout: float,
        follow_redirects: bool,
    ) -> Iterator[httpx.Response]:
        """Preserve the existing stream interface while bounding the complete network exchange."""
        portal = self._portal
        if portal is None or self.is_closed:
            raise RuntimeError("Generation client is closed")
        if follow_redirects or not isfinite(timeout) or timeout <= 0:
            raise ValueError("Expected explicit routing and a positive remaining deadline")
        try:
            response = portal.call(
                self._exchange, method, url, json, headers, monotonic() + timeout
            )
        except TimeoutError as error:
            raise httpx.ReadTimeout("Generation deadline exceeded") from error
        try:
            yield response
        finally:
            response.close()

    async def _exchange(
        self, method: str, url: str, body: Any, headers: dict[str, str], deadline: float
    ) -> httpx.Response:
        """Cancel headers and body together; never decode compressed or unbounded model data."""
        client, remaining = self._client, deadline - monotonic()
        if client is None:
            raise RuntimeError("Generation client is closed")
        if remaining <= 0:
            raise TimeoutError("Generation deadline exceeded")
        with anyio.fail_after(remaining):
            async with client.stream(
                method,
                url,
                json=body,
                headers=headers,
                timeout=remaining,
                follow_redirects=False,
            ) as response:
                response.raise_for_status()
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise ValueError("Encoded model response")
                payload = bytearray()
                async for block in response.aiter_bytes():
                    payload.extend(block)
                    if len(payload) > 1_000_000:
                        raise ValueError("Model response exceeds budget")
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=bytes(payload),
                    request=response.request,
                )
