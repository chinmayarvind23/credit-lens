# CreditLens High-Level Design

## Implemented demonstration

HF HTML entry page → embedded TypeScript workbench → HTTPS Quick Tunnel → local
FastAPI demo → current SQL grant → scoped memory catalog → local lexical retrieval
→ Decimal calculations and cited extracts → permission/citation checks → SQL audit.
Redis retrieval caching is optional and disabled by default. The live synthetic
demo requires the owner's computer, Docker and tunnel to remain running. No AWS
resources are deployed. The cloud flows below describe the intended architecture.

The local demo can opt into PostgreSQL on its grant/audit database. That path
shares canonical evidence and revocation between API instances; Redis remains
optional acceleration. Actual-service tests cover cross-instance cache reuse,
revocation, restart and audit rejection during a concurrent change. Process-local
quotas and demo bootstrap privileges still limit production scaling.

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
