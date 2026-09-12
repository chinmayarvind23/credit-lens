# Recovery

Restore under a closed-access maintenance boundary.

## Restore an operator deployment

1. Keep query ingress closed and workers stopped. Preserve the incident state and
   identify a trusted database backup, immutable source-object backup and their
   recovery timestamp. Restore into a new isolated database, not an active one.
2. Verify backup hashes and restore with error-stop/transactional options. Compare
   schema, row counts, canonical source hashes and protected audit packet hashes.
   Restore database roles and infrastructure configuration through their separate
   operator procedures; a database-only logical dump does not establish them.
3. Reconcile grants, subject disablement, document revocations and policy changes
   since the backup against a trusted current authority. Keep access closed if that
   record is unavailable. A valid signed token or the restored grant table alone
   cannot prove that a later revocation was preserved.
4. Verify immutable PDF bytes against canonical hashes. Inspect in-flight ingestion
   jobs and leases; fenced SQL state remains authoritative over duplicate queue
   notifications. Exercise a scoped ingestion/review recovery case before starting
   workers. Do not automatically approve restored OCR quarantine.
5. Rebuild search indexes and validate scoped search/citation canaries against the
   restored canonical catalog. Discard stale response/retrieval caches. Shared
   quota namespaces must remain consistent across workers; nonpersistent Redis
   restarts reset the abuse-control window and are not durable billing recovery.
6. Test an allowed query, a denied subject, revoked evidence, a missing source and
   fresh audit persistence. Verify readiness, metrics and alert queries. Reopen
   traffic only after current authorization and canonical consistency are proven.


See [Redis](../infra/redis/README.md) and [ingestion](../infra/postgres/README.md).
