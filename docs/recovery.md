# Recovery verification and operator runbook

The local PostgreSQL drill restored a logical backup into an empty database after
dropping its owned source database. Every saved table row hash matched, including
330 canonical pages/chunks, current grants, catalog authority and protected audits.
Saved document revocation and a disabled subject survived. A post-restore query
created a fresh audit whose protected packet hash was recomputed. A separate
source-object restore checked exact bytes and rejected tampering.

The drill deliberately revoked another grant after taking the backup. That change
was absent after restoration, and the restored grant was accepted. A consistent
backup is therefore insufficient to establish current permission authority.

## Reproduce the isolated drill

Use the pinned PostgreSQL image already cached locally. The test requires explicit
opt-in, creates a uniquely labeled container with a random loopback port, limits
memory/CPU, and removes only its own container. It never uses an existing database
URL or downloads an image.

```powershell
$env:CREDITLENS_RUN_RECOVERY_DRILL = '1'
python -m pytest tests/test_postgres_recovery.py -q -s
Remove-Item Env:CREDITLENS_RUN_RECOVERY_DRILL
```

The retained verification used isolated source `fe06a0e` plus the reviewed recovery
test: one test passed in 10.59 seconds. Its record is in the private project
resources under `audit/recovery-2026-09-11.md`. This is a functional local drill,
not a production recovery-time objective.

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

The drill does not establish point-in-time recovery, backup retention, encrypted
off-site storage, automatic failover, production roles or post-backup grant
reconciliation. Those require the operator's deployment and authoritative records.
AWS remains optional setup; no AWS resources were provisioned for this verification.

See [shared Redis quotas](../infra/redis/README.md),
[indexing canaries](../infra/monitoring/indexing.md),
[durable queue behavior](../infra/sqs/README.md) and [architecture](system-design.md).
