"""Bounded process-local packet reuse; callers still authorize, validate and audit every hit."""

from collections import OrderedDict
from collections.abc import Callable
from hashlib import sha256
from threading import RLock
from time import monotonic

from creditlens.domain import Packet, Principal, QueryRequest


class ResponseCache:
    """A cache belongs to one immutable workflow/provider configuration, never across releases."""

    def __init__(
        self, *, capacity: int = 128, ttl: float = 60, clock: Callable[[], float] = monotonic
    ) -> None:
        """Limit retained private packets and their lifetime without any external service."""
        if not 1 <= capacity <= 512 or not 1 <= ttl <= 3600:
            raise ValueError("Response cache capacity or TTL exceeds supported bounds")
        self.capacity = capacity
        self.ttl = ttl
        self.clock = clock
        self._entries: OrderedDict[str, tuple[float, Packet]] = OrderedDict()
        self._lock = RLock()

    @staticmethod
    def key(query: QueryRequest, principal: Principal, revision: int) -> str:
        """Bind the full grant, exact question/date and catalog epoch."""
        return sha256(
            f"packet-v1\n{principal.model_dump_json()}\n{query.model_dump_json()}\n{revision}".encode()
        ).hexdigest()

    def get(self, key: str) -> Packet | None:
        """Expire before reuse and update LRU order under the same lock as mutation."""
        with self._lock:
            item = self._entries.pop(key, None)
            if item is None or self.clock() >= item[0]:
                return None
            self._entries[key] = item
            return item[1]

    def put(self, key: str, packet: Packet) -> None:
        """Only callers with a completed audit may populate this bounded immutable value store."""
        if len(packet.model_dump_json().encode()) > 200_000:
            return
        with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = (self.clock() + self.ttl, packet)
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)
