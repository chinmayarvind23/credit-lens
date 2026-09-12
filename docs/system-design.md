# System design

CreditLens is a modular monolith with a TypeScript workbench. The server produces extractive underwriting packets: cited source text, deterministic financial calculations, missing inputs, conflicts and next actions. It keeps the final lending decision with the reviewer.

## Request flow

Authentication verifies the configured issuer and token contract. Authorization resolves current SQL grants rather than accepting borrower, role or tenant authority from the request body. Scope filters apply before ranking and cover tenant, borrower, access groups and effective dates.

Search providers return candidates. The canonical catalog validates their identity and supplies trusted text. The default path is lexical; optional local hybrid retrieval combines lexical and embedding ranks, applies selective query grounding and reranks within authorized candidates. Cortex and OpenSearch adapters use the same provider boundary.

The workflow routes policy-reference questions separately from borrower calculations. Decimal computes financial values from compatible inputs. Missing or conflicting evidence produces an explicit disposition. Exact citation validation and a final grant/catalog check precede protected audit persistence.

Response-cache keys bind the request, current principal and catalog revision. A hit still receives current evidence validation, a fresh request ID and a new audit. Redis retrieval caching stores signed identifiers that are rehydrated from canonical evidence. Optional Redis quotas coordinate request admission across processes and fail closed when the shared store is unavailable.

## Ingestion

Immutable source bytes enter a trusted store. PostgreSQL owns idempotent jobs and fenced leases. The digital parser runs in a restricted container; the parent verifies source metadata and hashes before transactional publication. Queue notifications wake workers while durable SQL job state remains authoritative.

The optional native OCR worker accepts trusted operator inputs, verifies generation completion and stages extraction in quarantine. A scoped reviewer can approve, correct or reject the retained artifact. Publication checks both reviewer and submitter grants under database locks and commits the pages and job state together.

## Deployment boundaries

The free Hugging Face demo runs the Python workflow in a browser worker with public fictional fixtures and session SQLite. Browser-side scope checks are demonstration behavior, not a confidentiality boundary against the visitor.

The server uses authenticated authority for protected data. PostgreSQL, Redis, search adapters, monitoring and queue integrations are optional configured service paths. The Cortex adapter has a local contract harness; the browser does not call it. The AWS folder supplies operator-managed infrastructure configuration.

See [runtime contracts](LLD.md), [security](security.md), [recovery](recovery.md) and [deployment](deployment.md).
