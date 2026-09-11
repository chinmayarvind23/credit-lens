"""Bounded Redis values contain authenticated identifiers, never permission authority."""

import hashlib
import hmac
import json
import re
from typing import Annotated, Literal, Protocol, cast
from urllib.parse import urlsplit

from pydantic import Field
from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from creditlens.domain import StrictModel

MAX_CACHE_BYTES = 32768
ChunkID = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CachedIDs(StrictModel):
    """Validate signed cache fields before any ID can resolve to canonical source text."""

    version: Literal[1] = 1
    key: str
    expires_at: float = Field(gt=0, allow_inf_nan=False)
    chunk_ids: tuple[ChunkID, ...] = Field(max_length=100)
    provider_mode: str = Field(min_length=1, max_length=200)


class CacheUnavailable(Exception):
    """A cache outage may bypass acceleration without bypassing authorization."""


class ByteCache(Protocol):
    """Keep real transport failures distinct from invalid signed values."""

    def get(self, key: str) -> bytes | None:
        """Return a bounded value or raise a curated cache availability failure."""
        ...

    def put(self, key: str, value: bytes, ttl: int) -> None:
        """Store one bounded value with a finite server-side lifetime."""
        ...


class RedisBytes:
    """Use a bounded pool and no retries because a cache miss has an authorized fallback."""

    def __init__(self, url: str) -> None:
        """Require TLS off loopback and keep credentials inside the Redis client's owned pool."""
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("redis", "rediss")
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Expected a Redis URL")
        if parsed.scheme == "redis" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Remote Redis connections require TLS")
        self.client = Redis.from_url(
            url,
            decode_responses=False,
            max_connections=16,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
            retry=Retry(NoBackoff(), 0),
            health_check_interval=15,
        )

    def get(self, key: str) -> bytes | None:
        """GETRANGE bounds even a malicious oversized value before it enters process memory."""
        try:
            value = cast(bytes, self.client.getrange(key, 0, MAX_CACHE_BYTES))
        except RedisError as error:
            raise CacheUnavailable("Cache read unavailable") from error
        return value or None

    def put(self, key: str, value: bytes, ttl: int) -> None:
        """Reject oversized writes and require TTL so stale identifiers cannot persist forever."""
        if len(value) > MAX_CACHE_BYTES or not 1 <= ttl <= 3600:
            raise ValueError("Invalid cache size or lifetime")
        try:
            self.client.set(key, value, ex=ttl)
        except RedisError as error:
            raise CacheUnavailable("Cache write unavailable") from error

    def close(self) -> None:
        """Release client-owned sockets during application or integration-test shutdown."""
        self.client.close()


def sign_entry(entry: CachedIDs, secret: bytes) -> bytes:
    """Bind IDs, provider identity, expiry and request key under a runtime-only signing key."""
    payload = entry.model_dump_json()
    signature = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    value = json.dumps({"payload": payload, "signature": signature}).encode()
    if len(value) > MAX_CACHE_BYTES:
        raise ValueError("Cache value exceeds its bound")
    return value


def read_entry(value: bytes, key: str, secret: bytes, now: float) -> CachedIDs:
    """Reject replay across keys, unsigned edits, duplicate IDs and expired payloads."""
    if len(value) > MAX_CACHE_BYTES:
        raise ValueError("Cache value exceeds its bound")
    envelope = json.loads(value)
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature"}:
        raise ValueError("Invalid cache envelope")
    payload, signature = envelope["payload"], envelope["signature"]
    if not isinstance(payload, str) or not isinstance(signature, str):
        raise ValueError("Invalid cache signature fields")
    if not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise ValueError("Invalid cache signature encoding")
    expected = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise ValueError("Invalid cache signature")
    entry = CachedIDs.model_validate_json(payload)
    if (
        entry.key != key
        or entry.expires_at <= now
        or len(set(entry.chunk_ids)) != len(entry.chunk_ids)
    ):
        raise ValueError("Stale, duplicate or mismatched cache entry")
    return entry
