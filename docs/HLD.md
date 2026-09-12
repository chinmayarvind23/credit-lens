# CreditLens High-Level Design

## Implemented demonstration

The free HF Space serves the TypeScript workbench and pinned Pyodide runtime.
The Python lexical workflow runs in a browser worker with public fictional evidence
and session SQLite. It needs no tunnel or owner computer. Browser scope checks are
for demonstration only; confidential evidence must never be shipped as an asset.

The FastAPI server shares the same core logic and adds managed identity, optional
hybrid retrieval, shared SQL catalog, Redis ID caching and digital ingestion workers.
An opt-in process-local response cache skips repeated retrieval/calculation while
reauthorizing, validating exact current sources and recording a fresh audit.
Local OTel spans and Prometheus metrics are implemented; remote monitoring is not.
AWS is an optional documented deployment and no resources have been provisioned.
The cloud flows below describe the broader planned architecture.

The local demo can opt into PostgreSQL on its grant/audit database. That path
shares canonical evidence and revocation between API instances. Redis integrations
remain optional. Actual-service tests cover cross-instance cache reuse,
revocation, restart and audit rejection during a concurrent change. Optional shared
Redis quotas enforce admission across API instances with fail-closed outage behavior and expiring opaque identity counters. Demo bootstrap privileges and
production deployment validation still limit production scaling.

Scanned-document work runs in a separate local CPU environment. A bounded Poppler
container renders physical pages, and pinned PaddleOCR-VL 1.6 with PP-DocLayoutV3
produces layout and recognized text for review. OCR artifacts retain source hashes
and scope but receive zero extraction confidence until reviewed; they cannot enter
retrieval automatically. A scoped administrator can inspect the durable artifact
and approve corrected text through the API or CLI. Canonical publication and the
review decision commit together. Public uploads and automatic OCR queue execution
remain unfinished. See [OCR experiments](../infra/ocr/README.md).

The durable ingestion store now uses PostgreSQL for intent, idempotency, leases,
retries, review state and completion. A worker's expired token cannot publish.
Digital evidence and completed job status share one transaction, with the current
admin grant locked through commit. Opt-in admin HTTP routes register staged source
hashes and read durable status with current grants. Local source storage separates
tenants and verifies immutable PDF bytes. Digital workers parse one source in a
network-disabled Docker container, recheck grants on heartbeats and publish only
validated output. An optional boto3 adapter now sends and consumes job notifications
through a local SQS-compatible broker. The opt-in API sends notifications after
committing intent. A persistent worker consumes them, acknowledges terminal jobs
and recovers SQL work on empty polls or broker outages. Shutdown intent prevents
a new claim after a pending broker poll and preserves its notification.
Managed SQS deployment and object storage remain unfinished. The database remains
authoritative for source identity and permissions.

## User flow

```text
Underwriter
   |
   v
Vercel Web App
   |
   v
Cognito / OIDC
   |
   v
FastAPI
   |
   +--> authorize tenant + borrower + ACL
   |
   +--> Snowflake Cortex Search
   |
   +--> Snowpark / SQL finance tools
   |
   +--> structured generation
   |
   +--> citation validation
   |
   v
Underwriting Packet
```

## Ingestion flow

```text
Admin / setup
   |
   v
S3 source document
   |
   v
parse or OCR
   |
   v
canonical pages
   |
   v
semantic chunks + provenance + ACL
   |
   v
embeddings
   |
   v
Snowflake / Cortex

After MVP:
same job coordinated through SQS with durable status.
```

## Retrieval laboratory

```text
Canonical chunks
   |
   +--> NumPy exact cosine
   +--> FAISS
   +--> OpenSearch BM25
   +--> Weaviate HNSW
   |
   v
same qrels / same benchmark harness
```

## Trust boundaries

1. Browser is untrusted.
2. Identity-provider token is verified by backend.
3. Tenant and ACL scope are derived from trusted application state.
4. Search filters are constructed server-side.
5. LLM never controls authorization scope.
6. General logs/traces avoid raw sensitive document content.

## Local hybrid request integration

The provider layer can compose scoped local ranking, lexical/dense rank fusion
and a bounded reranker. Every model boundary preserves current canonical evidence
and SQL grant checks; reranking retains the underlying branch snapshots. The
default local build remains lexical. The public demo uses a runtime that verifies pinned
model files and loads CPU models during startup; it bounds scoring concurrency
and retained document vectors. Composed evaluation and public deployment are
separate promotion steps. The Linux container reproduced all 240 frozen rankings
and workflow outcomes before its public HF browser verification. Offline model
results are reported separately from HTTP performance and semantic answer quality.
