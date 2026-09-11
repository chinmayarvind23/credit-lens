"""Signed cache codecs and transport configuration reject malformed or unsafe values."""

import json
from unittest.mock import Mock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from creditlens.cache import CachedIDs, CacheUnavailable, RedisBytes, read_entry, sign_entry


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "redis://example.com",
        "redis://",
        "redis://localhost?socket_timeout=600",
        "rediss://example.com#fragment",
    ],
)
def test_redis_url_configuration(url: str) -> None:
    """Untrusted URL options cannot disable transport security or override bounded timeouts."""
    with pytest.raises(ValueError):
        RedisBytes(url)


def test_redis_transport_bounds_and_errors() -> None:
    """GETRANGE bounds returned bytes and curated outages never expose credentials to callers."""
    backend = RedisBytes("redis://127.0.0.1:16389/15")
    client = Mock()
    backend.client = client
    client.getrange.return_value = b""
    assert backend.get("key") is None
    client.getrange.assert_called_once_with("key", 0, 32768)
    client.getrange.side_effect = RedisConnectionError("private provider message")
    with pytest.raises(CacheUnavailable, match="Cache read unavailable"):
        backend.get("key")
    client.set.side_effect = RedisConnectionError("private provider message")
    with pytest.raises(CacheUnavailable, match="Cache write unavailable"):
        backend.put("key", b"small", 60)
    for value, ttl in ((b"x" * 32769, 60), (b"small", 0), (b"small", 3601)):
        with pytest.raises(ValueError):
            backend.put("key", value, ttl)
    client.set.side_effect = None
    backend.put("key", b"small", 60)
    client.set.assert_called_with("key", b"small", ex=60)
    backend.close()
    client.close.assert_called_once()


@pytest.mark.parametrize(
    "envelope",
    [[], {}, {"payload": 1, "signature": "a" * 64}, {"payload": "x", "signature": "a" * 64}],
)
def test_invalid_signed_envelopes(envelope: object) -> None:
    """Invalid shape, field types and authenticators fail before source ID hydration."""
    with pytest.raises(ValueError):
        read_entry(json.dumps(envelope).encode(), "key", b"x" * 32, 1000)


def test_signed_key_replay_duplicate_and_size() -> None:
    """A valid signature cannot authorize a different key or duplicate evidence identities."""
    secret = b"x" * 32
    entry = CachedIDs(key="key", expires_at=1100, chunk_ids=("a" * 64,), provider_mode="local")
    with pytest.raises(ValueError):
        read_entry(sign_entry(entry, secret), "another-key", secret, 1000)
    duplicate = entry.model_copy(update={"chunk_ids": ("a" * 64, "a" * 64)})
    with pytest.raises(ValueError):
        read_entry(sign_entry(duplicate, secret), "key", secret, 1000)
    with pytest.raises(ValueError):
        sign_entry(entry.model_copy(update={"key": "x" * 32769}), secret)
