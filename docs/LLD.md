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
