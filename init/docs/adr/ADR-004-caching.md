# ADR-004: Scoped Redis Caching

## Status

Accepted after MVP.

## Decision

Use Redis for embedding cache, retrieval cache, and short-lived state.

Do not enable broad final-answer caching by default.

Every retrieval-cache key includes tenant, borrower, ACL fingerprint, index version, and retrieval config.

## Tradeoff

Caching reduces repeated compute and latency but introduces stale-data and invalidation risk.
