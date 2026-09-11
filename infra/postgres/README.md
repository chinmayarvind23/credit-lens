# Shared canonical evidence tests

`SqlEvidenceCatalog` stores immutable pages/chunks, page ACLs, revocations and a
durable authority UUID plus revision in PostgreSQL. Writers lock one state row
and publish atomically. A snapshot reads filtered records and their revision in
one SELECT. Revocation removes all sibling chunks from future snapshots and
invalidates results held by another process. Duplicate publication and repeated
revocation are idempotent; a revoked page cannot be restored by replaying a batch.

The default runtime still uses the in-memory catalog. This module is separately
verified before integration; it does not enable the unfinished production path.
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

The credential is disposable synthetic test data. Tests require loopback and the
exact `creditlens_test` database name, allocate unique catalogs and never drop
unrelated tables. The pinned cached PostgreSQL 16.4 image is a reproducible local
fixture, not a current production patch recommendation. No AWS resources are used.

The main manual CI job includes the same real PostgreSQL checks in core and
critical coverage gates. Its workflow is not dispatched under the no-spend
restriction. Database statement and lock waits are bounded at five and two seconds.
SQL failures roll back and return a curated catalog-unavailable error. The caller
must configure finite connection/pool timeouts; integration tests use a three-second
connect timeout. A whole-workflow deadline and production migration orchestration
remain separate work.
