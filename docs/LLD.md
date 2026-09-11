# CreditLens Low-Level Design

## Current runtime contracts

`domain.py` is authoritative for implemented schemas; the sketches below describe
the broader design. The public query accepts only borrower_id, question and an
effective date. Frozen Pydantic models reject extra fields. The server resolves
identity and current grants, validates cited canonical chunks and writes an audit
before returning a packet. The browser keeps tokens in memory, uses same-origin
fetch with a 30-second deadline, and binds source GET requests to the completed
packet's borrower/date. Native fetch is invoked as a standalone function to avoid
the browser's invalid-receiver error. Source dialogs render literal text.

The live HF entry page embeds the whole application, so UI and API share one
tunnel origin. Its release manifest and exact remote inventory are checked before
each parent-bound publication. Redis contains signed, bounded evidence IDs keyed
by the complete current scope, query, policy date, catalog and provider version;
hits rehydrate current evidence and produce a new audit. The optional PostgreSQL
catalog joins state, canonical pages/chunks and normalized ACLs in one statement,
serializes writers on a state row and invalidates old epochs across instances.
Full response caching and the following broader pipeline remain unfinished.

`ocr.py` normalizes at most 1 MB of offline vendor JSON into an immutable
`ScannedPage`. It validates image dimensions, up to 256 blocks, physical bounding
boxes and contiguous non-null reading order. Table markup remains literal data;
the raw output hash binds the fuller polygon/layout record retained by the worker.
Trusted metadata supplies tenant, borrower, ACL, version and physical page, while
its text is discarded. Extracted text receives a new content hash and zero
confidence. The renderer supplies PDF and bitmap hashes. Unverified generation
completion, invalid geometry and blank content produce an `ExtractionError`.
The experiment supervisor bounds elapsed time and logs and kills the complete
owned Windows process tree. This is not yet an untrusted-file production worker.

`ingestion_jobs.py` stores sanitized `IngestionInput` and a fingerprint behind a
unique queue/tenant/subject/idempotency key. `JobLease` is internal and holds a
random token; `JobStatus` exposes only state, attempt count, curated error and
timestamps. Claims use `FOR UPDATE SKIP LOCKED`, database clock and at most three
attempts. Mutations require the current token and unexpired lease, checked after
lock acquisition. Publication holds the job lock, locks the submitter grant for
share, validates all immutable page metadata and calls
`SqlEvidenceCatalog.publish_in_transaction`. The connection must use the same
pool, an active transaction and READ COMMITTED isolation. A final lease check
precedes completion; failure rolls back both page insertion and job state.

The admin POST requires `Idempotency-Key` and returns a `JobStatus` with HTTP 202;
the status GET parses a UUID and reauthorizes every read. Ingestion requires the
opt-in PostgreSQL demo catalog and a synthetic queue namespace. The default public
underwriter receives 403. An authorized admin receives 503 when ingestion is
disabled. Submissions share the process-local authenticated request limiter;
these controls do not yet establish distributed quotas or source upload isolation.

`source_store.py` hashes the tenant namespace and accepts at most 25 MB per PDF.
It fsyncs a temporary file, atomically links the immutable object and verifies
regular-file type, containment, size and SHA-256 on every read. Its configured
root is trusted host storage. Interrupted submission can leave unreferenced
objects; automatic garbage collection is not implemented.

`ingestion_worker.py` claims digital jobs only. It checks the current SQL grant
before source access and on each heartbeat. Docker receives one read-only input
directory, a pinned local image ID, no network, user 1000, dropped capabilities,
a read-only root, 512 MiB, one CPU and 64 PIDs. Parent supervision and an inner
GNU timeout bound parsing to 1..120 seconds. The parent polls stdout/stderr sizes
every 100 ms and rejects more than 8 MiB/1 MB; these are detection thresholds,
not a filesystem quota. Docker logging is disabled and only the owned random
container is removed. Temporary files are deleted on normal cleanup; host failure
and source-store retention require operator maintenance.

`pdf_worker.py` reads and hashes the exact bytes passed to `ingest_pdf_bytes`.
It discards metadata text and returns actual text, hashes and pypdf provenance.
Digital extraction confidence 1 means text extraction succeeded, not measured
semantic accuracy. The parent verifies source identity and immutable metadata
again before atomic publication. Malformed or altered sources fail; missing
objects, timeouts and Docker launch failures retry. Lease loss cannot acknowledge
another worker's work. OCR stays queued for its separate reviewed path.

`scripts/ingest_documents.py` is a trusted local operator interface. It uses the
initialized API's database, queue and catalog configuration, resolves an existing
private admin subject, validates manifest scope before reading the source and
checks grants again after staging. It creates no grants. Separate invocations
submit idempotently and execute one eligible digital job. It prints bounded
status or curated errors; it does not implement remote user authentication.

`sqs_queue.py` restricts the current executable adapter to numeric loopback HTTP
and a synthetic CreditLens queue on the same origin. boto3 uses explicit synthetic
credentials, no proxy and finite transport/retry limits. `Notification` accepts
only a job UUID; bounded `Delivery` bodies and receipt handles are not logged.
`queue_worker.py` runs the existing fenced worker, deletes only terminal jobs and
defers active/retry notifications. Queue-scoped status reads cannot select a job
from another configured SQL queue. Unknown or malformed messages remain for a
dead-letter policy. Three consecutive broker failures open a 30-second process-local
circuit. A lock protects circuit updates across concurrent API background tasks;
network calls run outside that lock. Operator submission sends after SQL commit
and reports `PENDING` on failure. The opt-in API schedules sending after its 202
response; a failure logs a curated code and leaves durable intent unchanged.

`worker_loop.py` reuses clients, paces iterations and automatically polls SQL
when notifications are absent or the broker is unavailable. Invalid notifications
and storage failures produce curated events without acknowledgment. SIGINT,
SIGTERM or a trusted stop-file requests stopping after current work. The consumer
checks stop intent after a broker poll before claiming any job, leaving a received
message unacknowledged. The loop is not an OS service or crash supervisor. The
synthetic local-only queue configuration remains opt-in; managed AWS configuration
is unfinished. See [queue integration](../infra/sqs/README.md).

## Core domain schemas

Illustrative contracts:

```python
class Citation(BaseModel):
    document_id: str
    document_version: str
    page: int
    chunk_id: str

class FinancialMetric(BaseModel):
    name: str
    value: Decimal
    unit: str | None
    source_fields: list[str]

class Claim(BaseModel):
    text: str
    citations: list[Citation]

class UnderwritingPacket(BaseModel):
    borrower_summary: str
    calculated_metrics: list[FinancialMetric]
    applicable_policy: list[Claim]
    policy_disposition: PolicyDisposition
    missing_documents: list[str]
    exceptions: list[Claim]
    contradictions: list[Claim]
    recommended_next_actions: list[str]
    questions_for_underwriter: list[str]
    abstained: bool
```

## Retrieval request contract

```python
class RetrievalRequest(BaseModel):
    borrower_id: UUID
    query: str
    effective_at: datetime | None = None
    top_k: int = 20
```

Tenant, user, role, and ACL scope are injected from trusted request context.

## Evidence contract

```python
class RetrievedEvidence(BaseModel):
    chunk_id: UUID
    document_id: UUID
    document_version: str
    page: int
    section: str | None
    text: str
    dense_score: float | None
    lexical_score: float | None
    fused_score: float | None
    rerank_score: float | None
```

## Retrieval stages

1. normalize query,
2. classify exact vs semantic vs follow-up,
3. derive trusted metadata filters,
4. retrieve candidates,
5. fuse if hybrid path,
6. rerank,
7. deduplicate,
8. enforce context budget,
9. attach immutable provenance.

## RRF baseline

```python
def reciprocal_rank_fusion(
    rankings: list[list[str]],
    k: int = 60,
) -> dict[str, float]:
    scores: dict[str, float] = {}

    for ranking in rankings:
        for rank, document_id in enumerate(ranking, start=1):
            scores[document_id] = (
                scores.get(document_id, 0.0)
                + 1.0 / (k + rank)
            )

    return scores
```

## Cache keys

Embedding cache:

`hash(normalized_text | embedding_model | embedding_version)`

Retrieval cache:

`tenant | borrower | ACL_fingerprint | query_hash | index_version | retrieval_config`

Never cache across tenants or authorization scopes.

## Typed errors

Define application errors for authentication, authorization, missing borrower, unsupported document, extraction failure, search unavailable, insufficient evidence, stale index, invalid citation, model unavailable, and dependency timeout.

## Retry policy

Retries are bounded and only applied to operations safe to retry.

Mutating operations use idempotency where required.

## Test seams

Keep injectable interfaces around search provider, reranker, model provider, OCR provider, object store, cache, and clock.

Do not create abstraction layers with no real testing or benchmark purpose.
