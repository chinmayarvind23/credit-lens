# ADR-004: Scoped Redis Caching

## Status

Accepted after MVP.

## Decision

Use Redis for embedding cache, retrieval cache, and short-lived state.

Do not enable broad final-answer caching by default.

Every retrieval-cache key includes tenant, borrower, ACL fingerprint, index version, and retrieval config.

## Tradeoff

Caching reduces repeated compute and latency but introduces stale-data and invalidation risk.


## Implemented response-cache option

The server now also supports an opt-in, bounded process-local packet LRU. It caches
only accepted audited packets and binds the full grant/query/date/catalog epoch.
Every hit revalidates current evidence, checks grants again and creates a fresh ID
and audit. Provider changes rebuild the workflow and cache. This is separate from
Redis ID caching; embedding caching remains planned. It is disabled by default.
Local HTTP p95 fell from 35.78 to 29.21 ms in the recorded five-borrower comparison.
No production/cloud speedup or cost saving is claimed.
