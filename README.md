# CreditLens

**Production RAG for regulated commercial lending**

Status: implementation in progress from a design-only starting point. Quality,
latency, cost and corpus-size figures in the PRD are targets, not measured results.
No public deployment is available yet.

CreditLens helps commercial-loan underwriters assemble policy-grounded borrower evidence, deterministic financial metrics, missing-document checks, policy exceptions, conflicts, and recommended next actions with page-level citations. The final lending decision remains human-controlled.

## Core product loop

1. Authenticate an underwriter.
2. Resolve tenant, borrower scope, role, and ACLs.
3. Ingest policy and borrower documents with page-level provenance.
4. Retrieve only authorized evidence.
5. Compute financial metrics deterministically.
6. Generate a structured underwriting packet.
7. Validate every citation and policy version.
8. Abstain or request more evidence when support is insufficient.
9. Record trace, metrics, and audit evidence.

## Architecture principle

Start as a **modular monolith** because the query path is tightly coupled and the modeled workload is modest. Keeping the backend stateless so horizontal scaling stays straightforward. Adding supporting infrastructure only when a measured requirement justifies it.

```text
Browser / Vercel
      |
TypeScript + Bun
      |
     REST
      |
FastAPI modular monolith on AWS
      |
      +--> Cognito / OIDC identity
      +--> Snowflake Cortex Search
      +--> Snowpark / SQL calculations
      +--> S3 source documents
      +--> Redis cache
      +--> RDS metadata
      +--> OpenSearch lexical/operational search
      +--> SQS async ingestion jobs
      +--> OTel / LangSmith / Prometheus / CloudWatch
```

The retrieval lab uses the same canonical chunks to compare exact NumPy search, FAISS, OpenSearch BM25, Weaviate HNSW, dense retrieval, hybrid fusion, and reranking.

## Documentation

- [PRD](PRD.md)
- [System Design](docs/system-design.md)
- [HLD](docs/HLD.md)
- [LLD](docs/LLD.md)
- [Evaluation](docs/evaluation.md)
- [Security](docs/security.md)
- [Observability](docs/observability.md)
- [Performance](docs/performance.md)
- [Failure Modes](docs/failure-modes.md)
- [Deployment](docs/deployment.md)
- [Commands](docs/commands.md)

Execution plans, agent instructions and private evidence are maintained in the
parent workspace at `resources/credit_lens`.

## Development setup

Install Python tooling with uv, then run `uv sync --locked` in this directory.
The project pins Python 3.11. Frontend setup is in [apps/web](apps/web/README.md).
Run `uv run ruff check src`, `uv run ruff format --check src` and
`uv run mypy src` to verify the initial Python environment.
