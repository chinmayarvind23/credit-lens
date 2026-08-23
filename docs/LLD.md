# CreditLens Low-Level Design

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

The human should write or closely review this implementation.

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
