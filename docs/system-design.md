# CreditLens System Design

## 1. Problem restatement

Design a permission-aware commercial-lending RAG system that combines borrower documents, structured borrower data, and versioned lender policy to produce a cited underwriting-preparation packet without allowing the model to make the final lending decision.

## 2. Functional and non-functional requirements

See `PRD.md`.

Key non-functional constraints:

- retrieval-time ACL enforcement,
- page-level provenance,
- version-aware policy lookup,
- deterministic financial calculations,
- reproducible RAG evaluation,
- explicit latency/cost measurement,
- dependency resilience,
- no unsupported production-scale claims.

## 3. Assumptions

- Small underwriting-team workload.
- Synthetic borrower data for public portfolio use.
- One primary AWS region for the portfolio deployment.
- Snowflake is externally managed.
- Final loan decision remains human-controlled.
- Search index refresh may be eventually consistent.
- Authorization changes cannot rely on stale cached scope.

## 4. Back-of-the-envelope workload

Modeled:

- 50 registered underwriters,
- 10 active concurrently,
- 25 queries per underwriter per day,
- 1,250 queries/day.

The core backend does not need Kubernetes or separate microservices for this load.

The backend stays stateless so an ALB plus additional ECS tasks is the first horizontal-scaling step.

## 5. API contracts

### Public query API

`POST /api/v1/query`

Input:

- borrower ID,
- natural-language question,
- optional effective date.

Identity and ACL come from trusted authentication state.

Output:

- structured underwriting response,
- request ID,
- evidence references,
- policy disposition,
- abstention state.

### Underwriting packet

`POST /api/v1/underwriting-packet`

### Ingestion

`POST /api/v1/admin/documents`

MVP may process synchronously for a small corpus. Productionized path returns a job ID.

### Job status

`GET /api/v1/admin/index-jobs/{job_id}`

### Health

- `/health`
- `/ready`
- `/metrics`

## 6. Data model

### Durable source of truth

S3:

- original borrower documents,
- original policy documents,
- benchmark artifacts.

Snowflake:

- governed structured borrower data,
- canonical document/chunk metadata,
- searchable evidence,
- Snowpark transformations.

RDS/Postgres:

- application metadata,
- indexing jobs,
- selected eval metadata,
- operational state needing OLTP semantics.

Redis:

- embedding cache,
- retrieval cache,
- short-lived state.

OpenSearch:

- lexical retrieval benchmark/support path,
- operational/audit search where appropriate.

Weaviate:

- HNSW benchmark path.

## 7. Chosen high-level architecture

```text
                               +--------------------+
                               |   Vercel Web App   |
                               | TypeScript + Bun   |
                               +---------+----------+
                                         |
                                       HTTPS
                                         |
                               +---------v----------+
                               |      FastAPI       |
                               |  Modular Monolith  |
                               +---------+----------+
                                         |
             +---------------------------+---------------------------+
             |                           |                           |
      +------v------+             +------v------+             +------v------+
      | Cognito/OIDC |             | Snowflake   |             |     S3      |
      | auth identity|             | Cortex/SQL  |             | raw docs    |
      +-------------+             +------+------+             +-------------+
                                         |
                                  +------+------+
                                  | Snowpark    |
                                  | calculations|
                                  +-------------+

After MVP:
FastAPI -> Redis
FastAPI -> RDS
FastAPI -> OpenSearch
FastAPI -> SQS workers
FastAPI -> OTel/LangSmith/CloudWatch
```

## 8. Why modular monolith

The query path is tightly coupled:

`auth -> retrieval -> deterministic tools -> generation -> validation`

Splitting it early would create network calls, service auth, distributed tracing complexity, retry semantics, and version mismatch risk with no present scale justification.

Extract a service only if it develops independent scaling, availability, ownership, or deployment requirements.

## 9. Retrieval architecture

### Production path

Snowflake Cortex Search with mandatory metadata filters.

### Shadow retrieval laboratory

Same canonical chunks feed:

- exact NumPy vector search,
- FAISS,
- OpenSearch BM25,
- Weaviate HNSW.

This separates production governance from retrieval-learning experiments.

## 10. Retrieval-time authorization

Required order:

```text
authenticate
-> resolve tenant/borrower/ACL
-> construct mandatory metadata filter
-> retrieve
-> rerank
-> context build
-> LLM
```

Forbidden:

```text
retrieve broad corpus
-> rerank
-> filter unauthorized evidence
```

Hard invariant:

`UnauthorizedRetrievedChunks == 0`

## 11. Hybrid retrieval

Dense retrieval handles semantic similarity.

BM25 handles exact policy terms, forms, codes, identifiers, and uncommon domain phrases.

RRF is the default fusion baseline because it combines rank positions without requiring raw BM25 and vector-score calibration.

`RRF(d) = sum_r 1 / (k + rank_r(d))`

RRF discards score magnitude, so the cross-encoder reranker performs final fine-grained relevance ordering.

## 12. Query transformations

Selective HyDE is used only if the query is semantically vague and lacks exact identifiers.

Follow-up rewriting handles conversational references.

MMR remains benchmark-only until it proves useful.

## 13. Context construction

Context builder must:

- preserve top evidence,
- deduplicate near-identical chunks,
- preserve relevant exception text,
- stay within token budget,
- carry page/source metadata,
- reject unauthorized evidence,
- track exact context presented to the model.

## 14. Deterministic calculation path

Financial calculations run outside the LLM.

Example:

`DSCR = Net Operating Income / Total Debt Service`

The LLM receives typed results and may explain them.

Execution-path eval fails if a case requiring deterministic arithmetic is answered through unverified model arithmetic.

## 15. Structured generation

Output contains:

- borrower summary,
- metrics,
- requirements,
- policy disposition,
- exceptions,
- missing evidence,
- conflicts,
- recommended next actions,
- citations,
- human-review questions,
- abstention state.

## 16. Citation validation

For each claim:

1. citation exists,
2. document exists,
3. page exists,
4. chunk exists,
5. chunk was retrieved,
6. chunk was authorized,
7. policy version is applicable,
8. evidence supports the claim.

Steps 1-7 are deterministic where possible. Step 8 may use deterministic rules plus an LLM judge.

## 17. Consistency choices

Require current/strong semantics for:

- ACL and permission changes,
- structured data used for calculations,
- policy effective-date resolution,
- durable audit writes.

Eventual consistency is acceptable for:

- search refresh,
- dashboards,
- aggregate metrics.

Index freshness is surfaced explicitly.

## 18. Durability choices

Durable:

- source documents,
- document versions,
- structured borrower data,
- audit events,
- gold eval data,
- benchmark artifacts.

Rebuildable:

- embeddings,
- indexes,
- caches,
- temporary OCR artifacts.

## 19. Caching

Embedding cache is keyed by content hash plus embedding version.

Retrieval cache is scoped by tenant, borrower, ACL fingerprint, query, index version, and retrieval configuration.

Final-answer caching is disabled by default.

Redis failure degrades to uncached behavior where safe.

## 20. Async ingestion

```text
upload
-> persist source
-> enqueue SQS job
-> parse/OCR
-> chunk
-> embed
-> load
-> refresh index
-> verify
-> mark complete
```

The interactive query path remains synchronous.

## 21. Failure handling

### Cortex unavailable

Bounded retry, timeout, optional authorized fallback only if its freshness/security contract is satisfied, otherwise explicit unavailable response.

Never generate without evidence.

### Redis unavailable

Treat as cache miss and emit degraded telemetry.

### LLM unavailable

Return retryable error or evidence-only result. Do not fabricate.

### OCR failure

Mark extraction failed and do not silently index corrupted output.

### Index lag

Expose `indexed_at`, `index_version`, and freshness status.

### Traffic spike

Scale stateless ECS tasks behind ALB before redesigning the application.

## 22. Observability

Trace stages:

- auth,
- query classification,
- query rewrite,
- retrieval,
- rerank,
- calculation,
- generation,
- citation validation,
- policy validation.

Do not put raw borrower PII into general telemetry by default.

## 23. Scale evolution

1. tune current service,
2. vertical scale,
3. cache repeated expensive work,
4. async expensive work,
5. horizontal FastAPI scaling,
6. read replicas or service extraction if measured bottlenecks justify them.

## 24. Architecture alternatives rejected

### All-Snowflake

Simple governance, but weak first-principles retrieval/HNSW learning surface.

### Fully self-managed search stack

Maximum retrieval control, but more infrastructure and weaker governed-enterprise primary path.

### Early microservices

Independent deployment/scaling, but unjustified complexity for a tightly coupled, low-QPS portfolio workload.

Chosen design uses Snowflake as primary governed path plus a shadow retrieval lab and modular monolith.
