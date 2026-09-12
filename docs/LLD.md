# CreditLens Low-Level Design

## Current runtime contracts

`domain.py` is authoritative for implemented schemas; the sketches below describe the broader
design. The public query accepts only borrower_id, question and an effective date. Frozen Pydantic
models reject extra fields. An omitted date defaults to today. The server resolves identity and
current grants, validates cited canonical chunks and writes an audit before returning a packet. The
browser keeps tokens in memory, uses same-origin fetch with a 30-second deadline, and binds source
GET requests to the completed packet's borrower/date. Native fetch is invoked as a standalone
function to avoid the browser's invalid-receiver error. Source dialogs render literal text.

The free HF browser build hosts the existing Python workflow in a worker. The bridge receives JSON
data and returns JSON strings, preserving null rather than Pyodide's direct None-to-undefined
conversion. Only public synthetic pages are bundled. The worker has no managed credentials or server
API. Its startup and query deadlines are 120 and 30 seconds; late responses are discarded after
cancellation.

`ResponseCache` is an opt-in per-workflow immutable packet LRU with at most 512 entries, a maximum
200 KB packet and TTL up to one hour. Keys bind the complete principal grant, exact query/date and
catalog revision. Runtime provider changes require a new workflow/cache. A hit verifies every source
against current candidates, rechecks grants and revision, validates citations, assigns a fresh
request ID and writes a new protected audit. Only successfully audited cold results are cached.
Concurrent cold requests may both compute; stampede suppression is not implemented.

Redis caches signed evidence IDs, separately from response caching. Its hits hydrate current
canonical pages. PostgreSQL serializes catalog changes and invalidates old epochs across instances.
The local response cache is per process. Request quotas default to per process; an explicit quota
Redis URL enables shared atomic counters with finite TTL and 503 on unavailability. All instances
must share namespace, limit and window configuration. Full identity digests avoid raw identity keys;
bounded maxmemory with noeviction is required so memory pressure rejects admission instead of
resetting counters. `TelemetryMiddleware` creates an OTel request root; workflow stages become child
spans. An allowlisted local exporter rotates JSONL files, and aggregate Prometheus metrics require
an admin grant. No query text or exception payload is exported.

`ocr.py` normalizes at most 1 MB of offline vendor JSON into an immutable `ScannedPage`. It
validates image dimensions, up to 256 blocks, physical bounding boxes and contiguous non-null
reading order. Table markup remains literal data; the raw output hash binds the fuller
polygon/layout record retained by the worker. Trusted metadata supplies tenant, borrower, ACL,
version and physical page, while its text is discarded. Extracted text receives a new content hash
and zero confidence. The renderer supplies PDF and bitmap hashes. Unverified generation completion,
invalid geometry and blank content produce an `ExtractionError`. The experiment supervisor bounds
elapsed time and logs and kills the complete owned Windows process tree. The opt-in
`infra/ocr/worker.py` integration independently verifies raw EOS sequences before normalization and
commits durable REVIEW_REQUIRED state. Its native model process is for trusted operator inputs; it
has no filesystem sandbox for public untrusted-file processing.

`ingestion_jobs.py` stores sanitized `IngestionInput` and a fingerprint behind a unique
queue/tenant/subject/idempotency key. `JobLease` is internal and holds a random token; `JobStatus`
exposes only state, attempt count, curated error and timestamps. Claims use `FOR UPDATE SKIP
LOCKED`, database clock and at most three attempts. Mutations require the current token and
unexpired lease, checked after lock acquisition. Publication holds the job lock, locks the submitter
grant for share, validates all immutable page metadata and calls
`SqlEvidenceCatalog.publish_in_transaction`. The connection must use the same pool, an active
transaction and READ COMMITTED isolation. A final lease check precedes completion; failure rolls
back both page insertion and job state.

The admin POST requires `Idempotency-Key` and returns a `JobStatus` with HTTP 202; the status GET
parses a UUID and reauthorizes every read. Ingestion requires the opt-in PostgreSQL demo catalog and
a synthetic queue namespace. The default public underwriter receives 403. An authorized admin
receives 503 when ingestion is disabled. Submissions share the configured authenticated request
limiter, including optional shared Redis quotas. These controls do not establish source upload
isolation.

`source_store.py` hashes the tenant namespace and accepts at most 25 MB per PDF. It fsyncs a
temporary file, atomically links the immutable object and verifies regular-file type, containment,
size and SHA-256 on every read. Its configured root is trusted host storage. Interrupted submission
can leave unreferenced objects; automatic garbage collection is not implemented.

`ingestion_worker.py` claims digital jobs only. It checks the current SQL grant before source access
and on each heartbeat. Docker receives one read-only input directory, a pinned local image ID, no
network, user 1000, dropped capabilities, a read-only root, 512 MiB, one CPU and 64 PIDs. Parent
supervision and an inner GNU timeout bound parsing to 1..120 seconds. The parent polls stdout/stderr
sizes every 100 ms and rejects more than 8 MiB/1 MB; these are detection thresholds, not a
filesystem quota. Docker logging is disabled and only the owned random container is removed.
Temporary files are deleted on normal cleanup; host failure and source-store retention require
operator maintenance.

`pdf_worker.py` reads and hashes the exact bytes passed to `ingest_pdf_bytes`. It discards metadata
text and returns actual text, hashes and pypdf provenance. Digital extraction confidence 1 means
text extraction succeeded, not measured semantic accuracy. The parent verifies source identity and
immutable metadata again before atomic publication. Malformed or altered sources fail; missing
objects, timeouts and Docker launch failures retry. Lease loss cannot acknowledge another worker's
work. Configured native OCR extraction stages quarantine, then a hash-bound admin decision
atomically publishes reviewed pages. Both reviewer and original submitter grants are checked under
database locks. Rejection retains the artifact without publishing evidence.

`scripts/ingest_documents.py` is a trusted local operator interface. It uses the initialized API's
database, queue and catalog configuration, resolves an existing private admin subject, validates
manifest scope before reading the source and checks grants again after staging. It creates no
grants. Separate invocations submit idempotently and execute one eligible digital job. It prints
bounded status or curated errors; it does not implement remote user authentication.

`sqs_queue.py` restricts the current executable adapter to numeric loopback HTTP and a synthetic
CreditLens queue on the same origin. boto3 uses explicit synthetic credentials, no proxy and finite
transport/retry limits. `Notification` accepts only a job UUID; bounded `Delivery` bodies and
receipt handles are not logged. `queue_worker.py` runs the existing fenced worker, deletes only
terminal jobs and defers active/retry notifications. Queue-scoped status reads cannot select a job
from another configured SQL queue. Unknown or malformed messages remain for a dead-letter policy.
Three consecutive broker failures open a 30-second process-local circuit. A lock protects circuit
updates across concurrent API background tasks; network calls run outside that lock. Operator
submission sends after SQL commit and reports `PENDING` on failure. The opt-in API schedules sending
after its 202 response; a failure logs a curated code and leaves durable intent unchanged.

`worker_loop.py` reuses clients, paces iterations and automatically polls SQL when notifications are
absent or the broker is unavailable. Invalid notifications and storage failures produce curated
events without acknowledgment. SIGINT, SIGTERM or a trusted stop-file requests stopping after
current work. The consumer checks stop intent after a broker poll before claiming any job, leaving a
received message unacknowledged. The loop is not an OS service or crash supervisor. The synthetic
local-only queue configuration remains opt-in; managed AWS configuration is unfinished. See [queue
integration](../infra/sqs/README.md).

## Authoritative contracts and optional integrations

Use [`domain.py`](../src/creditlens/domain.py) and generated OpenAPI for exact schemas.
`QueryRequest` accepts borrower ID, bounded question and effective date; `Packet` carries typed
claims, Decimal calculations, disposition, missing evidence, conflicts, next actions and exact
citations. Caller-supplied tenant, role and ACL fields are rejected. Financial calculations stay
outside model inference.

The API exposes `/api/v1/query` and its `/api/v1/underwriting-packet` alias, scoped
`/api/v1/evidence/{chunk_id}`, privileged staged-ingestion/review routes, `/health`, `/ready` and
protected `/api/v1/metrics`. Admin submission returns 202 with a durable job ID and requires an
idempotency key; it is not a public PDF upload.

`graphql_admin.py` provides an optional read-only explorer at `/api/v1/admin/graphql`. It bounds
expansion and pagination, rechecks current admin scope and drops partial results on resolver
failure. It excludes document text, protected packets, other subjects' audits and lease tokens.
`infra/rpc` defines a versioned protobuf transport with deadlines and message bounds. It reuses
current authentication, workflow and quota behavior; the executable demo is loopback-only.

`apps/web` can optionally read the public Supabase directory. Only HTTPS Supabase origins and
publishable keys are accepted. A three-second deadline, 16 KiB response bound and exact fixture
validation precede use; failure falls back to bundled labels. The SQL migration permits only
published labels and denies client writes. This optional directory cannot change evidence scope or
server authorization.

`ingestion_metrics.py` reads four bounded SQL gauges for retained job counts, attempts, oldest state
age and expired leases. Metric labels exclude subjects, borrowers and source identities. The local
Grafana dashboard combines these with OTel/Prometheus request and workflow observations; six rules
cover availability, latency, server/citation failures and ingestion backlog/expired leases. Five
SQL-backed panels and two ingestion alerts were checked against a real local fixture with explicitly
injected old timestamps. This is fault-drill evidence, not observed production backlog.

Shared quotas use one parameterized Lua EVAL to check and increment a subject counter with its
initial TTL. Full SHA-256 subject keys have fixed length. Configure bounded Redis memory with
noeviction and identical namespace/limit/window across workers. A quota outage returns 503 without
workflow execution; an exhausted allowance returns 429. No automatic retry or local fallback can
double-spend an ambiguous admission. Server TTL and reconnection recover automatically; restarting
Redis without persisted counters resets windows. Retrieval-cache outages may recompute authorized
work, so their failure policy is deliberately separate.

Retries remain bounded; publication and review require current fenced leases or hash-bound
decisions. Test seams surround actual search, cache, OCR, queue and SQL boundaries. See [Redis
verification](../infra/redis/README.md), [monitoring](../infra/monitoring/README.md),
[gRPC](../infra/rpc/README.md) and [Supabase](../infra/supabase/README.md).

## Bounded reranking provider

`LocalSearchProvider` accepts an injected scorer only after resolving current SQL grants and
selecting canonical tenant, borrower, ACL and policy-date scope. The result validator rejects
duplicates, altered records, foreign chunks and excessive results. `RerankProvider` takes at most 40
candidates from its wrapped provider and returns at most ten. It retains the original hybrid result,
verifies both branches before and after scoring and delegates exact citations to their source.
Failure in a required scorer fails the request. These tested provider contracts are complemented by
a verified optional Linux CPU container. The free browser and default local builds remain lexical.
The local model runtime audit identifies the model manifest and full hybrid/reranker chain; the
packet's `local-extractive` label continues to describe answer assembly.

`CREDITLENS_RETRIEVAL_MODE=hybrid` requires an explicit local model directory in demo mode.
`LocalNeuralRanker` verifies the packaged 17-file manifest before offline-only safetensors loading.
It uses four CPU threads, one concurrent inference, a 4,096-entry document-vector LRU and no query
cache. Vector keys bind tenant, chunk identity and actual text hash; each vector owns its memory.
Dense input is limited to 512 scoped chunks and reranking to 40 pairs. Token truncation is
explicitly 256 for embeddings and 512 for pairs. See the complete budgets, setup and evaluation
scope in [local retrieval](../infra/retrieval/README.md). Both optional Redis and model references
are owned by application lifespan. Packet admission also requires a non-generic question term in
authorized ranked text. Nearest-neighbor output and common words alone cannot establish relevance.
This conservative textual floor can reject zero-overlap semantic paraphrases and does not validate
every qualifier; calibrated semantic abstention remains open.

## Monitoring and recovery evidence

The current bundle provisions 23 operational, six evaluation and nine indexing panels. Runtime
counters separate known/unknown dollar costs, and the histogram excludes unknowns. Actual scrapes
verified an unknown count with an empty cost mean; Grafana proxy checks verified six indexing and
two whole-packet expressions. Evaluation panels import reconciled artifacts and do not assert human
calibration.

The indexing snapshot acknowledged 3,840 chunks in 0.957 seconds, with one canary searchable after
0.990 seconds and a separate deliberate rejection. Its recorded LF-normalized metrics preserve the
original report and raw bytes. This is not a measurement of continuous production freshness or a
natural model-error rate.

A separate backup/restore test preserved 330 canonical rows, protected audits and snapshot
revocations. Changes after the snapshot, including revocations, require operator reconciliation
before reopening; follow the [recovery runbook](recovery.md).

`LocalNeuralRanker` receives the application-owned telemetry through `open_search`. Each encoder
and reranker invocation includes output validation inside its observed stage. Cached document
vectors skip encoding; a failed query releases the inference lock. Metrics carry only fixed stage
labels. The local SQL-publication experiment separately verifies every expected chunk's complete
searchable text and metadata in one successful sweep, with no union of incomplete sweeps.
See [neural monitoring](../infra/monitoring/neural.md) and
[publication visibility](../infra/monitoring/publication-visibility.md).

General policy-procedure lookups use the extractive path rather than calculating
DSCR from the selected borrower. The question-only rule requires procedural and
policy-topic cues; explicit borrower, assessment, or missing-document cues retain
financial review. Procedural cues use raw question words because retrieval
tokenization removes interrogatives.
This fixes irrelevant numerical dispositions, but does not add answer synthesis.
Frozen DeepEval results predate this change and remain unchanged.
