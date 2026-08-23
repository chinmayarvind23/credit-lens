# CreditLens 20-Chunk Plan

Each chunk includes:

- pre-implementation brief,
- development mode,
- flow diagram,
- tests/evals,
- risks,
- acceptance criteria,
- definition of done,
- evidence to preserve,
- interview probe.

## Phase 0: Design

### Chunk 1: Repo, environments, PRD, HLD, ADRs

**Mode:** Spec-driven

Goal:

- initialize Python/Bun environments,
- establish repo structure,
- freeze MVP and non-goals,
- capture capacity assumptions,
- define API contracts and schemas,
- compare architecture alternatives,
- mark target resume metrics.

Acceptance:

- `uv` environment works,
- Bun environment works,
- docs link correctly,
- target metrics are clearly labeled,
- no measured claim is invented.

## Phase 1: Walking skeleton

### Chunk 2: Deployed vertical slice

**Mode:** Spec-driven + TDD for contracts

Flow:

`Vercel -> FastAPI/AWS -> Snowflake -> browser`

Acceptance:

- public deployment works,
- frontend calls backend,
- backend reaches Snowflake,
- `/health` and `/ready` work,
- secrets are not committed.

### Chunk 3: Managed auth and authorization model

**Mode:** TDD/security

Flow:

`Cognito -> JWT validation -> tenant/borrower/ACL context`

Acceptance:

- valid token works,
- expired/invalid tokens fail,
- borrower scope exists in typed request context.

## Phase 2: Data foundation

### Chunk 4: Synthetic lending corpus and first 50 evals

**Mode:** Eval-driven

Acceptance:

- representative policy and borrower files exist,
- at least 50 gold cases exist,
- missing, contradiction, ACL, policy-version, and OCR cases are included.

### Chunk 5: Born-digital ingestion

**Mode:** TDD

Acceptance:

- page provenance,
- stable IDs,
- versions,
- content hashes,
- parser metadata.

### Chunk 6: Scanned/layout-heavy ingestion

**Mode:** TDD

Stack:

- PaddleOCR-VL,
- PP-DocLayoutV3,
- PP-OCRv6 fallback where justified.

Acceptance:

- scanned pages retain source linkage,
- tables/layout are tested,
- extraction failures are explicit.

## Phase 3: First useful RAG

### Chunk 7: Cortex dense baseline

**Mode:** Eval-driven

Acceptance:

- baseline qrels run saved,
- Recall@K/MRR/nDCG recorded,
- mandatory ACL filters applied before retrieval.

### Chunk 8: Structured underwriting packet

**Mode:** Eval-driven + TDD for calculations/validators

Adds:

- deterministic financial calculations,
- page citations,
- policy disposition,
- missing evidence,
- exceptions,
- recommended next actions,
- abstention.

**MVP achieved here.**

## Phase 4: Retrieval engineering

### Chunk 9: Chunking benchmark

**Mode:** Hypothesis -> baseline -> experiment -> eval

Compare:

- fixed/token,
- sentence-aware,
- semantic/structure-aware.

### Chunk 10: Lexical + dense hybrid

**Mode:** Eval-driven

Stack:

- OpenSearch BM25,
- dense retrieval,
- RRF,
- metadata filters.

Acceptance:

- hybrid result is compared to dense-only.

### Chunk 11: Reranking and context budget

**Mode:** Eval-driven

Compare:

- retrieval candidate count,
- rerank candidate count,
- final context chunks/tokens.

Measure quality, latency, token use, and cost.

### Chunk 12: Retrieval laboratory

**Mode:** Experiment

Stack:

- NumPy exact search,
- FAISS,
- Weaviate HNSW.

Measure:

- exact-vs-ANN agreement,
- recall,
- latency,
- indexing time,
- HNSW parameters.

### Chunk 13: Query transformations

**Mode:** Hypothesis -> baseline -> experiment -> eval

Test independently:

- selective HyDE,
- follow-up rewriting,
- optional MMR.

No technique is promoted without evidence.

## Phase 5: Evaluation engineering

### Chunk 14: 240-case adversarial harness

**Mode:** Eval-driven

Stack:

- DeepEval,
- RAGAS,
- deterministic validators,
- versioned LLM judge.

Report:

- outcome grade,
- execution-path grade,
- system metrics.

## Phase 6: Reliability and security

### Chunk 15: Security/failure suite

**Mode:** TDD/security

Cases:

- cross-tenant,
- wrong borrower,
- prompt injection,
- restricted docs,
- stale policy,
- missing docs,
- contradictions,
- bad OCR,
- context overflow.

Hard invariant:

`UnauthorizedRetrievedChunks == 0`

### Chunk 16: Redis and index lifecycle

**Mode:** TDD + benchmark

Adds:

- embedding cache,
- retrieval cache,
- index versioning,
- freshness,
- re-embedding migration,
- rollback.

### Chunk 17: Async ingestion and resilience

**Mode:** TDD

Stack:

- RDS/Postgres,
- SQS,
- retries,
- timeouts,
- circuit breakers,
- idempotency,
- job status.

## Phase 7: Productionization

### Chunk 18: Observability and primary cloud

**Mode:** Spec-driven + verify

Stack:

- OTel,
- LangSmith,
- Prometheus,
- Grafana,
- CloudWatch,
- Docker,
- Terraform,
- AWS deployment.

Acceptance:

- stage traces,
- dashboards,
- infrastructure plan,
- load/failure evidence.

### Chunk 19: Remaining required engineering paths

**Mode:** Spec/benchmark/TDD as appropriate

Implement meaningful roles for:

- gRPC internal contract,
- GraphQL admin/eval explorer,
- PySpark/Spark backfill benchmark,
- managed OpenSearch path,
- RDS,
- ElastiCache,
- EC2/ECS,
- Lightsail comparison,
- Supabase sanitized demo metadata if retained,
- CI/CD completion.

No MCP or A2A for CreditLens.

## Phase 8: Presentation

### Chunk 20: Polish and interview package

**Mode:** Verify/polish

Finish:

- README,
- HLD/LLD,
- ADRs,
- evaluation methodology,
- failure report,
- benchmark tables,
- security docs,
- observability screenshots,
- Vercel demo,
- HF Spaces sanitized demo,
- technical blog,
- 30-second pitch,
- 2-minute pitch,
- 10-minute deep dive,
- resume-bullet probes,
- system-design walkthrough,
- lessons learned.

Acceptance:

- clean clone works,
- tests/evals commands work,
- important evidence is linked,
- target metrics are replaced by measured values or remain clearly marked targets.
