# Governed document ingestion

Authorized operators can stage a PDF, submit a durable job and publish its extracted pages into the existing governed PostgreSQL catalog. The API and CLI select the same catalog and queue. Weaviate synchronization is a separate authorized step after canonical publication.

## Configure the publisher

Start with the complete production identity, catalog, search and [generation configuration](generation.md). Supply a current private admin grant covering the source tenant, borrower and every page ACL. The public demo identity must remain an underwriter.

```text
CREDITLENS_MODE=production
CREDITLENS_CATALOG_BACKEND=postgres
CREDITLENS_GOVERNED_CATALOG_ID=<existing-catalog-id>
CREDITLENS_INGESTION_ENABLED=true
CREDITLENS_INGESTION_QUEUE_ID=<fresh-governed-queue>
```

The queue name accepts letters, digits, underscores and hyphens. Production requires an explicit name outside the `synthetic-` namespace. Ingestion is disabled by default. API and worker processes must share the intended database, catalog and queue configuration.

Stop old ingestion workers before applying the job schema and application together. A trusted operator initializes the job tables against the already provisioned catalog:

```powershell
@'
from creditlens.ingestion_jobs import initialize_jobs
from creditlens.settings import Settings
from creditlens.sql_catalog import SqlEvidenceCatalog
from creditlens.storage import open_database

config = Settings()
engine = open_database(config.database_url)
try:
    SqlEvidenceCatalog(engine, config.governed_catalog_id)
    initialize_jobs(engine)
finally:
    engine.dispose()
'@ | uv run --no-sync python -
```

Governed API and worker startup require this existing schema and catalog. They do not seed demo pages, create grants or replace a missing catalog. The queue's first initialization records its catalog ID and immutable authority UUID. Reopening requires that exact pair. A recreated catalog, another catalog or an unbound worker cannot reuse the queue. Existing legacy jobs must be drained under their original configuration; choose a fresh governed queue instead of adopting them. A queue and its catalog must also share the same database connection pool within a process.

## Stage and process a PDF

Use a trusted source directory with operator-managed permissions, retention and disk capacity. Prepare an `IngestionInput` manifest using the generated `/docs` schema: exact PDF SHA-256, `parser: digital` and contiguous physical-page metadata with tenant, borrower, document/version, policy dates and ACL groups. Input text is discarded; extraction supplies canonical text. Corrections use a new document version rather than overwriting an immutable page identity.

Build or supply the reviewed local parser image and retain its immutable image ID. The worker requires a local digest and uses `--pull never`. The parser receives read-only source bytes without database credentials or network access.

```powershell
docker build --tag creditlens-pdf-parser:local .
$parserImage = docker image inspect --format '{{.Id}}' creditlens-pdf-parser:local
uv run --no-sync python -m scripts.ingest_documents --source-root C:/creditlens-sources submit --pdf C:/incoming/document.pdf --manifest C:/incoming/manifest.json --subject your-private-admin-subject --key document-v1
uv run --no-sync python -m scripts.ingest_documents --source-root C:/creditlens-sources work-one --image $parserImage --job-id <returned-job-id>
```

The CLI's subject is a lookup under trusted operator database access. It is not remote authentication. Submission rechecks and locks the current grant in its SQL transaction, including idempotent retries. The worker retains lease ownership, rechecks grants during parsing and verifies that extracted metadata matches the manifest. Canonical pages, catalog revision and completed job status commit together.

An empty queue returns `IDLE`. Failed jobs expose curated error codes; retryable work observes persisted availability times. `work-loop` can poll durable SQL jobs without a message broker. Optional notifications use the [local SQS adapter](../infra/sqs/README.md); SQL owns job state.

For authenticated API clients, `POST /api/v1/admin/documents` accepts the pre-staged source hash and manifest with an `Idempotency-Key` header. It returns status 202 and a job ID. It accepts no raw file upload and performs no extraction in the request. `GET /api/v1/admin/index-jobs/{job_id}` checks current admin scope. Optional [OCR](../infra/ocr/README.md#reviewed-admission) remains quarantined until scoped approval; API and CLI review publish only into the bound catalog.

## Make the published scope searchable

A `COMPLETED` ingestion job means that canonical SQL publication succeeded. It does not acknowledge vector visibility. With Weaviate selected, use the separate indexer credential and synchronize every affected authorized borrower and policy date following [Weaviate setup](weaviate.md):

```powershell
uv run --no-sync python -m scripts.index_weaviate --subject your-private-admin-subject --borrower <authorized-borrower-id> --effective-at <YYYY-MM-DD>
```

Serving uses the reader key. Keep writer credentials out of the API process. Both commands retain verified TLS and the configured model/catalog binding. Index synchronization rechecks authority around embedding and upload, and requires searchable coverage. Interrupted uploads can be retried without reparsing or duplicating canonical pages.

Current-scope queries fail until all required keys are visible. Tenant-wide policy publication can affect multiple borrower/date scopes; synchronize each intended serving scope before reopening it. Check the full authorized chunk budget, including policy, rather than using PDF page count as the capacity measure. Catalog revision changes invalidate stale cached evidence, and a previous packet cannot make an incomplete new scope searchable.
