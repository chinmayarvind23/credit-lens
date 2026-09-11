# Shared canonical evidence tests

`SqlEvidenceCatalog` stores immutable pages/chunks, page ACLs, revocations and a
durable authority UUID plus revision in PostgreSQL. Writers lock one state row
and publish atomically. A snapshot reads filtered records and their revision in
one SELECT. Revocation removes all sibling chunks from future snapshots and
invalidates results held by another process. Duplicate publication and repeated
revocation are idempotent; a revoked page cannot be restored by replaying a batch.

The default runtime still uses the in-memory catalog. Demo mode can opt into the
shared catalog on its existing PostgreSQL grant/audit engine; it does not enable
the unfinished production path. Startup idempotently publishes the synthetic
corpus under a synthetic-prefixed catalog ID and never restores revoked pages.
Bootstrap with `initialize_catalog(engine, catalog_id)` using an administrator
connection to an owned database. Readers construct `SqlEvidenceCatalog` without
creating schema. Mutation methods are internal administrator/ingestion operations,
not public API endpoints. Database credentials and role grants govern write access.
Readers assume the SQL database is authoritative and not modified outside this
publication protocol.

Run the actual database tests locally with the owned synthetic fixture:

```powershell
docker run -d --rm --name creditlens-postgres-check --memory 512m --cpus 1 -e POSTGRES_PASSWORD=creditlens-test-only -e POSTGRES_DB=creditlens_test -p 127.0.0.1:15432:5432 postgres:16.4-alpine@sha256:5660c2cbfea50c7a9127d17dc4e48543eedd3d7a41a595a2dfa572471e37e64c
$env:CREDITLENS_TEST_POSTGRES_URL='postgresql+psycopg://postgres:creditlens-test-only@127.0.0.1:15432/creditlens_test'
.venv\Scripts\pytest.exe infra/postgres/test_catalog.py --cov=creditlens.sql_catalog --cov-branch --cov-report=term-missing
docker stop creditlens-postgres-check
```

For the local demo, keep that database running and configure:

```powershell
$env:CREDITLENS_DATABASE_URL=$env:CREDITLENS_TEST_POSTGRES_URL
$env:CREDITLENS_CATALOG_BACKEND='postgres'
$env:CREDITLENS_DEMO_CATALOG_ID='synthetic-demo-v1'
.venv\Scripts\python.exe -m uvicorn creditlens.api:create_app --factory --host 127.0.0.1 --port 8000
```

The database must contain only synthetic demonstration data. A real deployment
needs explicit schema migration and database roles; demo startup uses bootstrap
privileges. Returning to the default mode requires removing these environment
overrides. The disposable command above has no durable volume, so stopping it
removes its test state. Use deliberate persistent storage for retained audits.

With the [Redis fixture](../redis/README.md) running, also set
`CREDITLENS_TEST_REDIS_URL` to exercise cache reuse between two real API instances.
The tests verify that a committed page revocation removes source access and
cached evidence on both instances, restart does not restore the page, revoked
grants deny both apps, and mid-query revocation prevents a success audit.

The credential is disposable synthetic test data. Tests require loopback and the
exact `creditlens_test` database name, allocate unique catalogs and never drop
unrelated tables. The pinned cached PostgreSQL 16.4 image is a reproducible local
fixture, not a current production patch recommendation. No AWS resources are used.

The main manual CI job includes the same real PostgreSQL checks in core and
critical coverage gates. Its workflow is not dispatched under the no-spend
restriction. Database statement and lock waits are bounded at five and two seconds.
SQL failures roll back and return a curated catalog-unavailable error. The caller
must configure finite connection/pool timeouts; application PostgreSQL engines use
a three-second connect timeout, five-second pool wait and bounded statements.
A whole-workflow deadline and production migration orchestration
remain separate work.

## Durable ingestion state

`creditlens.ingestion_jobs` now stores immutable, hash-addressed PDF intent and
bounded metadata in PostgreSQL. `initialize_jobs` explicitly creates its schema;
`JobStore` binds operations to a named queue. A current administrator must also
hold the tenant, borrower and every page ACL. Repeating the same subject-scoped
idempotency key returns the existing job; changed input returns a conflict.
Prefilled metadata text is discarded before storing the job.

Workers claim using `FOR UPDATE SKIP LOCKED`, database time and a fresh fencing
token. A 10..900-second lease defaults to 600 seconds. Heartbeats cannot revive an
expired lease. Retryable failures wait 10 seconds times the attempt number; a
third failure or expired third attempt becomes terminal. Public status contains
curated error codes, never source content, raw exceptions or lease tokens.

Digital publication locks the current submitter grant, validates the extracted
manifest and publishes through the catalog's transaction entry point. The page
inserts, catalog epoch and completed status commit together. A final lease check
rolls back publication if ownership expires during work. The shared grant lock
prevents concurrent revocation between authorization and commit. An OCR job can
record a hashed review artifact but cannot automatically complete or publish.

`test_ingestion_jobs.py` has 15 actual-database cases, including concurrent claim,
restart/retry recovery, permission changes, post-insert failure/expiry rollback
and a real conflicting grant lock. These tests currently invoke the job store
directly. HTTP submission/status, source-object handling, executable workers and
SQS delivery are the next integration work; the store alone does not implement
the complete async ingestion path or an AWS deployment.
