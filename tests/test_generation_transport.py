"""Verify real socket cancellation, transport ownership, and bounded model responses."""

import ipaddress
import json
import ssl
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from time import monotonic, sleep
from unittest.mock import Mock

import anyio
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from creditlens.generation_transport import GenerationClient


@pytest.fixture(params=[False, True], ids=["http", "verified-tls"])
def service(request, tmp_path):
    """Own a loopback service and trust its temporary TLS certificate only in this fixture."""
    calls = []

    class Handler(BaseHTTPRequestHandler):
        """Expose delayed headers and cumulative body delay with deterministic small JSON."""

        protocol_version = "HTTP/1.1"

        def handle(self):
            """Expected cancelled sockets must not emit irrelevant server tracebacks."""
            try:
                super().handle()
            except OSError:
                pass

        def do_GET(self):
            """Keep each body gap below the budget while their sum exceeds it."""
            calls.append(self.path)
            body = b'{"models":[]}'
            if self.path == "/headers":
                sleep(1.6)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.path == "/body":
                sleep(0.8)
                self.wfile.write(body[:1])
                self.wfile.flush()
                sleep(0.8)
                self.wfile.write(body[1:])
            else:
                self.wfile.write(body)
            self.wfile.flush()

        def log_message(self, format, *args):
            """Keep synthetic request details out of test output."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    verify = True
    if request.param:
        context, verify = local_tls(tmp_path)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        scheme = "https" if request.param else "http"
        yield f"{scheme}://127.0.0.1:{server.server_port}", verify, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def local_tls(directory):
    """Use a temporary self-signed fixture CA with verification enabled, never an insecure flag."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert, secret = directory / "fixture.crt", directory / "fixture.key"
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    secret.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, secret)
    return context, ssl.create_default_context(cafile=str(cert))


def request(client, url, timeout=1):
    """Exercise the same bounded synchronous interface used by the generator."""
    with client.stream(
        "GET",
        url,
        json=None,
        headers={"Accept-Encoding": "identity"},
        timeout=timeout,
        follow_redirects=False,
    ) as response:
        return response.json()


@pytest.mark.parametrize("path", ["/body", "/headers"])
def test_real_deadline_cancels_then_recovers(service, path):
    """Cancel both response phases on HTTP/TLS and preserve the shared client for the next call."""
    base, verify, calls = service
    with GenerationClient(verify=verify) as client:
        started = monotonic()
        with pytest.raises(httpx.TimeoutException):
            request(client, base + path)
        assert monotonic() - started < 1.4
        assert request(client, base + "/fast") == {"models": []}
        assert not client.is_closed
    assert client.is_closed and calls == [path, "/fast"]


@pytest.mark.parametrize(
    "change",
    [
        {"trust_env": True},
        {"follow_redirects": True},
        {"verify": False},
    ],
)
def test_unsafe_client_configuration_is_rejected(change):
    """Evidence-bearing transport cannot discover ambient proxies or disable TLS verification."""
    with pytest.raises(ValueError, match="verified TLS"):
        GenerationClient(**change)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout_performs_no_io(timeout):
    """Expired or unbounded caller budgets must never enter the async transport."""
    transport = httpx.MockTransport(Mock(side_effect=AssertionError("unexpected I/O")))
    with GenerationClient(transport=transport) as client:
        with pytest.raises(ValueError, match="positive remaining"):
            request(client, "http://127.0.0.1/", timeout)


@pytest.mark.parametrize("case", ["encoded", "oversized", "status", "redirect"])
def test_invalid_reply_is_rejected_and_client_recovers(case):
    """Bound bytes and representation before decoding, with no redirect or status fallback."""
    calls = []

    def reply(incoming):
        """Return one invalid upstream response followed by a valid response on the same client."""
        calls.append(incoming)
        if len(calls) > 1:
            return httpx.Response(200, json={"ok": True})
        if case == "encoded":
            return httpx.Response(200, content=b"{}", headers={"content-encoding": "custom"})
        if case == "oversized":
            return httpx.Response(200, content=b"x" * 1_000_001)
        if case == "redirect":
            return httpx.Response(302, headers={"location": "https://elsewhere.invalid/"})
        return httpx.Response(503)

    with GenerationClient(transport=httpx.MockTransport(reply)) as client:
        with pytest.raises((ValueError, httpx.HTTPStatusError)):
            request(client, "http://127.0.0.1/")
        assert request(client, "http://127.0.0.1/") == {"ok": True}
    assert len(calls) == 2


def test_expired_portal_budget_never_reaches_transport():
    """Queue delay cannot reset the original deadline when the async task starts."""
    transport = httpx.MockTransport(Mock(side_effect=AssertionError("unexpected I/O")))
    with GenerationClient(transport=transport) as client:
        assert client._portal is not None
        with pytest.raises(TimeoutError):
            client._portal.call(client._exchange, "GET", "http://127.0.0.1/", None, {}, 0)


def test_context_cleanup_and_no_use_outside_lifecycle():
    """Normal and exceptional ownership paths close idempotently and reject stale calls."""
    client = GenerationClient()
    with pytest.raises(RuntimeError, match="closed"):
        request(client, "http://127.0.0.1/")
    with pytest.raises(ValueError, match="caller failed"):
        with client:
            with pytest.raises(RuntimeError, match="already open"):
                client.__enter__()
            raise ValueError("caller failed")
    client.close()
    assert client.is_closed
    with pytest.raises(RuntimeError, match="closed"):
        anyio.run(client._exchange, "GET", "http://127.0.0.1/", None, {}, monotonic() + 1)


def test_production_factory_uses_bounded_adapter(monkeypatch):
    """The actual factory alias composes the adapter with normal model verification and cleanup."""
    from creditlens import runtime
    from creditlens.settings import Settings

    calls = []
    clients = []

    def reply(incoming):
        """Provide model metadata without inference, retaining methods and body validation."""
        calls.append(incoming)
        if incoming.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": "fixture:8b", "digest": "a" * 64}]}
            )
        assert json.loads(incoming.content) == {"model": "fixture:8b"}
        return httpx.Response(
            200, json={"details": {"format": "gguf"}, "capabilities": ["completion"]}
        )

    def factory(**kwargs):
        """Inject only the upstream wire, preserving production portal/client ownership."""
        client = GenerationClient(transport=httpx.MockTransport(reply), **kwargs)
        clients.append(client)
        return client

    assert runtime.GenerationClient is GenerationClient
    monkeypatch.setattr(runtime, "GenerationClient", factory)
    with runtime.open_generation(
        Settings(generation_model="fixture:8b", generation_digest="a" * 64)
    ) as generator:
        assert generator is not None
        generator.check_ready()
    assert clients[0].is_closed and len(calls) == 4
