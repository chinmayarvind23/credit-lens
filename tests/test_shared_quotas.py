"""Shared quota contracts and explicitly isolated real Redis recovery verification."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from redis.exceptions import ConnectionError

from creditlens.api import create_app
from creditlens.errors import ServiceError
from creditlens.limits import RedisQueryLimiter
from creditlens.settings import Settings


def test_shared_quota_fail_closed_and_opaque_keys() -> None:
    """A transport error cannot grant local fallback or disclose credentials in the wire error."""
    limiter = RedisQueryLimiter("redis://127.0.0.1:16390/15", "unit", 1, 1)
    try:
        assert "alice@example" not in limiter.key("alice@example")
        assert len(limiter.key("a")) == len(limiter.key("a" * 10000))
        limiter.backend.client.eval = Mock(side_effect=ConnectionError("sensitive"))
        with pytest.raises(ServiceError) as error:
            limiter.check("alice")
        assert error.value.status == 503 and "sensitive" not in error.value.message
    finally:
        limiter.close()


@pytest.mark.parametrize("value", [0, 3601])
def test_quota_window_is_finite(value: int) -> None:
    """Invalid lifetimes fail configuration before any server resource is opened."""
    with pytest.raises(ValidationError):
        Settings(query_window_seconds=value)


def test_http_quota_outage_prevents_work() -> None:
    """The configured shared path surfaces 503 without invoking packet generation."""
    settings = Settings(
        database_url="sqlite:///:memory:", quota_redis_url=SecretStr("redis://127.0.0.1:16390/15")
    )
    with TestClient(create_app(settings)) as client:
        client.app.state.limiter.backend.client.eval = Mock(side_effect=ConnectionError("secret"))
        client.app.state.workflow.query = Mock(side_effect=AssertionError("must not execute"))
        response = client.post(
            "/api/v1/query",
            json={"borrower_id": "borrower-001", "question": "DSCR", "effective_at": "2026-06-01"},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "quota_unavailable"
        assert not client.app.state.workflow.query.called


@pytest.mark.skipif(not os.getenv("CREDITLENS_TEST_QUOTA_REDIS_URL"), reason="owned Redis only")
def test_real_shared_quota_contention_expiry_and_recovery() -> None:
    """Only an owned disposable server may be paused; independent clients share one allowance."""
    url = os.environ["CREDITLENS_TEST_QUOTA_REDIS_URL"]
    namespace = "check-" + uuid4().hex
    first = RedisQueryLimiter(url, namespace, 7, 1)
    second = RedisQueryLimiter(url, namespace, 7, 1)

    def admit(index: int) -> int:
        """Return admission outcomes from competing independently pooled clients."""
        try:
            (first if index % 2 else second).check("synthetic-subject")
            return 200
        except ServiceError as error:
            return error.status

    try:
        with ThreadPoolExecutor(max_workers=12) as pool:
            statuses = list(pool.map(admit, range(30)))
        assert statuses.count(200) == 7 and statuses.count(429) == 23
        key = first.key("synthetic-subject")
        assert 0 < first.backend.client.pttl(key) <= 1000
        second.check("other-subject")
        first.close()
        first = RedisQueryLimiter(url, namespace, 7, 1)
        assert admit(1) == 429  # Application restart cannot reset the shared allowance.
        time.sleep(1.1)
        assert admit(1) == 200
        first.backend.client.set(key, "corrupt", px=1000)
        assert admit(1) == 503
        first.backend.client.delete(key)
        first.backend.client.set(key, 1)  # Missing expiry must never become an infinite quota.
        assert admit(1) == 503
        first.backend.client.delete(key)
        first.backend.client.execute_command("CLIENT", "PAUSE", 600, "ALL")
        assert admit(1) == 503
        time.sleep(0.7)
        assert admit(1) == 200
    finally:
        first.close()
        second.close()


@pytest.mark.skipif(not os.getenv("CREDITLENS_TEST_QUOTA_REDIS_URL"), reason="owned Redis only")
def test_real_shared_quota_across_http_apps() -> None:
    """Two separately initialized applications enforce one authenticated identity budget."""
    settings = Settings(
        database_url="sqlite:///:memory:",
        query_limit=1,
        quota_namespace="http-" + uuid4().hex,
        quota_redis_url=SecretStr(os.environ["CREDITLENS_TEST_QUOTA_REDIS_URL"]),
    )
    with TestClient(create_app(settings)) as first, TestClient(create_app(settings)) as second:
        request = {"borrower_id": "borrower-001", "question": "DSCR", "effective_at": "2026-06-01"}
        assert first.post("/api/v1/query", json=request).status_code == 200
        response = second.post("/api/v1/underwriting-packet", json=request)
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "rate_limited"
