"""Exercise the real gRPC transport with signed access tokens and durable synthetic audits."""

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr
from sqlalchemy import select, update

from creditlens.auth import Authenticator
from creditlens.corpus import build_demo_pages
from creditlens.domain import Packet
from creditlens.limits import QueryLimiter
from creditlens.response_cache import ResponseCache
from creditlens.retrieval import EvidenceCatalog
from creditlens.settings import Settings
from creditlens.storage import GrantStore, audit_events, grants, open_database
from creditlens.workflow import QueryWorkflow

# Core installs can omit the optional transport; the RPC acceptance command installs its extra.
grpc = pytest.importorskip("grpc")
from infra.rpc import creditlens_pb2, creditlens_pb2_grpc  # noqa: E402
from infra.rpc.service import UnderwritingService, serve_local  # noqa: E402


@pytest.fixture
def rpc():
    """Use actual RSA token validation and SQL grants while keeping the fixture on loopback."""
    with TemporaryDirectory(prefix="creditlens-rpc-") as directory:
        engine = open_database(f"sqlite:///{(Path(directory) / 'test.db').as_posix()}")
        store = GrantStore(engine)
        store.seed_demo()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        config = Settings(
            mode="production",
            issuer="https://cognito-idp.us-east-1.amazonaws.com/test-pool",
            client_id="test-client",
            cortex_url="https://example.test/search",
            cortex_token=SecretStr("synthetic-unused"),
        )
        auth = Authenticator(config, store, key_resolver=lambda token: key.public_key())
        now = datetime.now(UTC)
        token = jwt.encode(
            {
                "sub": "synthetic-demo",
                "iss": config.issuer,
                "iat": now,
                "exp": now + timedelta(minutes=5),
                "token_use": "access",
                "client_id": config.client_id,
                "scope": config.required_scope,
            },
            key,
            algorithm="RS256",
        )
        workflow = QueryWorkflow(
            EvidenceCatalog(build_demo_pages()), store, response_cache=ResponseCache()
        )
        service = UnderwritingService(auth, workflow, QueryLimiter())
        try:
            with serve_local(service) as target, grpc.insecure_channel(target) as channel:
                grpc.channel_ready_future(channel).result(timeout=5)
                yield creditlens_pb2_grpc.UnderwritingStub(channel), service, engine, token
        finally:
            engine.dispose()


def body(borrower="borrower-001", question="Prepare the DSCR underwriting packet"):
    """Send only the public query fields defined in the versioned protobuf contract."""
    return creditlens_pb2.QueryRequest(
        borrower_id=borrower, question=question, effective_at="2026-06-01"
    )


def query(rpc, request=None):
    """Use a finite deadline and an actual signed token on every successful-path RPC."""
    stub, _, _, token = rpc
    return stub.Query(
        request or body(), timeout=5, metadata=(("authorization", f"Bearer {token}"),)
    )


def test_real_packets_cache_and_fresh_audits(rpc):
    """Five lending states survive protobuf serialization; cache hits retain fresh SQL audit IDs."""
    first = [query(rpc, body(f"borrower-{i:03}")) for i in range(1, 6)]
    first.append(query(rpc, body(question="What is the minimum DSCR threshold?")))
    assert all(response.schema_version == 1 for response in first)
    packets = [Packet.model_validate_json(response.packet_json) for response in first]
    assert len({p.policy_disposition for p in packets}) == 5
    assert str(packets[0].calculated_metrics[0].value) == "1.5000"
    repeat = Packet.model_validate_json(query(rpc).packet_json)
    assert repeat.cache_hit and repeat.request_id != packets[0].request_id
    with rpc[2].connect() as connection:
        ids = set(connection.execute(select(audit_events.c.request_id)).scalars())
    assert ids == {p.request_id for p in [*packets, repeat]}


@pytest.mark.parametrize("authorization", [None, "Bearer invalid", "wrong-scheme"])
def test_invalid_identity_has_no_audit(rpc, authorization):
    """Unknown credentials cannot reach evidence generation or the protected audit writer."""
    metadata = () if authorization is None else (("authorization", authorization),)
    with pytest.raises(grpc.RpcError) as error:
        rpc[0].Query(body(), timeout=5, metadata=metadata)
    assert error.value.code() == grpc.StatusCode.UNAUTHENTICATED
    assert error.value.details() == "unauthenticated"
    with rpc[2].connect() as connection:
        assert connection.execute(select(audit_events.c.request_id)).first() is None


def test_current_grant_revocation_denies_warm_cache(rpc):
    """A valid unexpired token cannot authorize a cache hit after SQL revocation."""
    query(rpc)
    with rpc[2].begin() as connection:
        connection.execute(update(grants).values(enabled=False, revision=2))
    with pytest.raises(grpc.RpcError) as error:
        query(rpc)
    assert error.value.code() == grpc.StatusCode.PERMISSION_DENIED


def test_wrong_borrower_and_malformed_input(rpc):
    """The transport cannot add borrower scope or bypass Pydantic request constraints."""
    with pytest.raises(grpc.RpcError) as denied:
        query(rpc, body("borrower-999"))
    assert denied.value.code() == grpc.StatusCode.PERMISSION_DENIED
    for request in [
        body(question=""),
        creditlens_pb2.QueryRequest(
            borrower_id="borrower-001", question="Calculate DSCR", effective_at="invalid"
        ),
    ]:
        with pytest.raises(grpc.RpcError) as invalid:
            query(rpc, request)
        assert invalid.value.code() == grpc.StatusCode.INVALID_ARGUMENT


def test_missing_deadline_duplicate_metadata_and_size_bound(rpc):
    """Reject unbounded deadlines, ambiguous credentials and oversized protobuf messages."""
    with pytest.raises(grpc.RpcError) as missing:
        rpc[0].Query(body())
    assert missing.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    header = ("authorization", f"Bearer {rpc[3]}")
    with pytest.raises(grpc.RpcError) as duplicate:
        rpc[0].Query(body(), timeout=5, metadata=(header, header))
    assert duplicate.value.code() == grpc.StatusCode.UNAUTHENTICATED
    with pytest.raises(grpc.RpcError) as size:
        query(rpc, body(question="x" * 20000))
    assert size.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED


def test_quota_and_private_failure_text(rpc, monkeypatch):
    """Use the real limiter and keep arbitrary dependency exceptions off the RPC wire."""
    rpc[1].limiter = QueryLimiter(limit=1)
    query(rpc)
    with pytest.raises(grpc.RpcError) as limited:
        query(rpc)
    assert limited.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    rpc[1].limiter = QueryLimiter()

    def fail(*args):
        """Represent an uncategorized provider failure containing private diagnostic text."""
        raise RuntimeError("private-token-and-document")

    monkeypatch.setattr(rpc[1].workflow, "query", fail)
    with pytest.raises(grpc.RpcError) as private:
        query(rpc)
    assert private.value.code() == grpc.StatusCode.INTERNAL
    assert private.value.details() == "internal_error"


def test_client_deadline_cancels_delivery(rpc, monkeypatch):
    """Client cancellation bounds delivery while synchronous work may still finish its audit."""
    entered, release, completed = Event(), Event(), Event()
    original = rpc[1].workflow.query

    def delayed(*args):
        """Hold the actual workflow until the real client deadline has elapsed."""
        entered.set()
        release.wait(2)
        try:
            return original(*args)
        finally:
            completed.set()

    monkeypatch.setattr(rpc[1].workflow, "query", delayed)
    call = rpc[0].Query.future(
        body(), timeout=0.1, metadata=(("authorization", f"Bearer {rpc[3]}"),)
    )
    assert entered.wait(2)
    with pytest.raises(grpc.RpcError) as expired:
        call.result()
    assert expired.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    release.set()
    assert completed.wait(3)
    time.sleep(0.02)
