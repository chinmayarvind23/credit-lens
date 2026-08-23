# CreditLens PRD

## Executive summary

CreditLens is a production-oriented, permission-aware RAG system for commercial lending. It helps an underwriter assemble borrower evidence, compute deterministic financial metrics, identify relevant policy and exceptions, surface missing or conflicting evidence, and produce page-cited recommended next actions.

The product is scoped to **underwriting preparation and policy-grounded evidence synthesis** without autonomously approving/rejecting a loan.

## Problem statement

Commercial-loan underwriting requires analysts to reconcile policy documents, policy versions, borrower financial statements, bank statements, applications, debt schedules, structured customer data, and exception rules. Manual review is slow because evidence is fragmented and conclusions must be auditable.

CreditLens answers:

> Given this authorized borrower, the policy effective at this time, and all available evidence, what facts and financial metrics matter, what requirements apply, what evidence supports them, what is missing or contradictory, and what should the underwriter do next?

## Primary user

**Commercial-loan underwriter / credit analyst**

Secondary users:

- senior credit officer,
- compliance reviewer,
- AI/model reviewer.

Assumption: the institution already has a loan-origination system. CreditLens augments review rather than replacing the system of record.

## Product output

A structured underwriting packet containing:

- borrower summary,
- deterministic financial metrics,
- applicable policy requirements,
- policy disposition,
- missing documents,
- policy exceptions,
- evidence conflicts,
- recommended next actions,
- page-level citations,
- questions requiring human judgment,
- abstention state.

Allowed policy dispositions:

- `MEETS_POLICY`
- `EXCEPTION_REQUIRED`
- `INSUFFICIENT_EVIDENCE`
- `MATERIAL_CONFLICT`
- `HUMAN_JUDGMENT_REQUIRED`

CreditLens does not emit `APPROVE` or `REJECT` as a lending decision.

## One-sentence definition

CreditLens helps commercial-loan underwriters assemble policy-grounded borrower evidence, financial metrics, missing-document checks, policy exceptions, and recommended next actions with page-level citations while keeping the final lending decision human-controlled.

## MVP

Functional MVP requirements:

1. User authenticates through managed OIDC.
2. User selects a synthetic borrower within authorized scope.
3. System ingests lender-policy PDFs and borrower documents with page provenance.
4. User asks a lending or borrower question.
5. Retrieval is filtered by tenant, borrower, role, ACL, document version, and policy effective date before evidence reaches reranking or generation.
6. System returns a structured, grounded response with page-level citations.
7. Financial metrics are computed by deterministic Python, SQL, or Snowpark code.
8. Low-evidence cases return abstention, missing-evidence, or human-review state.

### MVP non-goals

These remain project requirements without blocking the MVP:

- GraphQL,
- gRPC,
- Weaviate HNSW tuning,
- FAISS benchmark,
- PySpark/Spark backfill benchmark,
- Supabase public-demo path,
- Lightsail deployment comparison,
- HF Spaces demo,
- full 240-case suite,
- full Grafana dashboards,
- load testing,
- multi-instance backend.

## Product validation

### Why RAG

The knowledge is private, versioned, changing, document-centric, citation-sensitive, and permission-sensitive. Fine-tuning alone does not solve dynamic provenance, freshness, or retrieval-time authorization.

### Why structured generation

Underwriters need a review artifact with explicit fields, not unconstrained prose.

### Why deterministic calculations

Ratios such as DSCR are deterministic business calculations. The model may explain them but should not be trusted to perform them.

## Competitive framing

### Generic enterprise search

Useful for document discovery but insufficient for deterministic calculations, policy disposition, exception handling, evidence reconciliation, and AI evals.

### Generic Ask-My-Docs RAG

Useful baseline but typically lacks borrower scope, version-aware policy logic, ACL-first retrieval, adversarial evals, and operational evidence.

### Lending AI platforms

Broader products may cover end-to-end lending. CreditLens focuses on a narrower, deeply evaluated underwriting-preparation slice.

## Data strategy

Use public/shareable policy material where allowed plus synthetic lender policy and synthetic borrower packages.

Synthetic policy should include:

- sections,
- exceptions,
- version history,
- future-effective policy,
- superseded policy,
- deliberate ambiguity.

Synthetic borrower packages should include:

- income statement,
- balance sheet,
- cash-flow statement,
- bank statements,
- debt schedule,
- application,
- collateral schedule,
- ownership documents,
- missing files handling,
- conflicting values handling,
- poor OCR handling,
- duplicate versions handling,
- stale versions handling.

Expected tables:

- borrowers,
- applications,
- loans,
- financial_periods,
- financial_metrics,
- documents,
- document_versions,
- chunks,
- policy_rules,
- indexing_jobs,
- audit_events,
- eval_runs,
- benchmark_runs.

## Functional requirements

### FR-01 Authentication

Use managed authentication. Default: AWS Cognito with OIDC/JWT.

### FR-02 Authorization

Resolve trusted tenant ID, user ID, role, borrower scope, and ACL groups. No trusting LLM-generated authorization arguments.

### FR-03 Document ingestion

Support born-digital PDF, scanned PDF, table extraction, and CSV/JSON structured data.

### FR-04 Provenance

Every canonical evidence chunk records stable document ID, document version, page, section, source span/bounding region where available, effective date, borrower or policy association, tenant, ACL groups, content hash, parser version, and chunker version.

### FR-05 Retrieval

Support dense retrieval, BM25/lexical retrieval, metadata filters, hybrid fusion, cross-encoder reranking, and selective query rewriting when evidence justifies it.

### FR-06 Deterministic calculations

Financial calculations run in typed Python, SQL, or Snowpark.

### FR-07 Structured answer

Use typed schemas for claims, citations, policy disposition, metrics, conflicts, and next actions.

### FR-08 Citation enforcement

Each factual policy or borrower claim must carry valid page-level provenance.

### FR-09 Abstention

Decline to infer when evidence is insufficient, stale, unauthorized, or contradictory.

### FR-10 Evaluation

Every important retrieval, prompt, model, or indexing change must be reproducibly evaluable.

### FR-11 Auditability

Persist enough request metadata to explain who asked, what scope applied, what evidence was retrieved, what versions were used, what answer was produced, and which validators passed.

### FR-12 Async ingestion

Post MVP, expensive ingestion and re-indexing move to durable background jobs with status endpoints.

## Non-functional requirements

### Security

Hard target:

- zero cross-tenant retrievals,
- zero unauthorized chunks entering model context,
- no raw secrets in source control,
- no sensitive borrower content in general-purpose telemetry by default.

### Quality

- Recall@10: 94.1%
- nDCG@10: 0.89
- citation precision: 96.8%
- grounded-answer pass rate: 92.5%
- unsupported-claim rate: 1.7%

### Latency

p95 query latency: 2.7 seconds after optimization.

### Cost

Average LLM-related cost/request: $0.043 after optimization.

### Reproducibility

Every benchmark stores configuration and version metadata.

### Durability

Durable:

- source documents,
- approved structured borrower data,
- audit events,
- gold eval datasets,
- benchmark results.

Rebuildable:

- embeddings,
- search indexes,
- caches,
- temporary OCR artifacts.

### Observability

Expose stage-level tracing and aggregate metrics for latency, errors, retrieval, cost, tokens, caching, indexing, and authorization failures.

## Capacity assumptions

Modeled workload:

- 50 registered underwriters,
- 10 concurrently active,
- 25 queries per underwriter per day,
- 1,250 queries/day.

Average query rate:

`1,250 / 86,400 ~= 0.0145 requests/s`

A 20x concentrated peak is still below 1 request/s.

The backend should start as a stateless modular monolith. ECS tasks can scale horizontally if load tests later justify it.

Final corpus: 3,800+ pages.

Record document count, pages, canonical chunks, embedding dimensions, index size, average chunk tokens, and ingestion throughput.

## Read/write profile

Interactive CreditLens is read-heavy.

Read path: policy lookup, borrower evidence, financial reads, retrieval, generation.

Write path: ingestion, document-version changes, audit events, eval results.

Optimize retrieval and precomputed indexes. Move heavy ingestion to async workers post-MVP.

## Consistency

Require current/strong semantics for:

- authorization,
- borrower values used in calculations,
- policy effective-date resolution,
- durable audit records.

Eventual consistency is acceptable for search refresh, dashboards, and aggregate metrics. Expose index freshness explicitly.

## Caching

### Embedding cache

Key includes normalized content hash and embedding model/version.

### Retrieval cache

Key includes tenant, borrower, ACL fingerprint, query representation, index version, and retrieval configuration.

### Final answer cache

Disabled by default. If experimentally enabled, scope by tenant, borrower, ACL, policy version, and index version with short TTL.

## Evaluation philosophy

Each eval case produces:

1. outcome grade,
2. execution-path grade,
3. system-performance metrics.

Outcome asks whether the result is correct.

Execution path asks whether the system reached it safely and correctly.

Deterministic checks should enforce hard invariants. LLM judges score semantic quality only where judgment is required.

## Golden set

Final suite: 240 questions.

| Category                     | Count |
| ---------------------------- | ----: |
| Direct policy lookup         |    60 |
| Borrower evidence            |    40 |
| Structured calculation       |    30 |
| Multi-document synthesis     |    25 |
| Missing documents            |    20 |
| Policy exceptions            |    20 |
| Contradictory evidence       |    15 |
| ACL / tenant isolation       |    10 |
| Stale/versioned policy       |    10 |
| Prompt injection/adversarial |    10 |
| Total                        |   240 |

## Completion Criteria

Complete when all 20 chunks meet acceptance criteria, all mandatory technology roles have evidence, the 240-case suite runs reproducibly, security suite passes, benchmark artifacts are saved, observability exists, the README and system docs are complete, non-functional requirements met, and a durable demo exists.
