# System design

CreditLens is a modular monolith with a TypeScript workbench. The server combines cited evidence, deterministic financial calculations, missing inputs, conflicts and next actions with separately labeled model-generated interpretation. It keeps the final lending decision with the reviewer.

## Request flow

Authentication verifies the configured issuer and token contract. Authorization resolves current SQL grants rather than accepting borrower, role or tenant authority from the request body. Scope filters apply before ranking and cover tenant, borrower, access groups and effective dates.

Search providers return candidates. The canonical catalog validates their identity and supplies trusted text. The demo defaults to lexical search. The authenticated production server selects Cortex Search or persistent Weaviate vector search. Weaviate combines canonical lexical ranks with vector ranks using reciprocal rank fusion, then reranks with pinned local models and applies query grounding. PostgreSQL remains the authority for every candidate. OpenSearch and local dense adapters share the same provider boundary for configured integrations.

The workflow routes policy-reference questions separately from borrower calculations. Decimal computes financial values from compatible inputs. Missing or conflicting evidence produces an explicit disposition. An abstained packet skips generation. Otherwise, the pinned Ollama generator receives selected authorized evidence and server calculations, returning statement text with evidence IDs and metric names. The server supplies full canonical chunks as supporting quotes, attaches all selected metric input citations and bounds the source union. The model must state a selected metric's exact numeric value. Model identity and current authority are checked before and after inference. Quote provenance and numeric presence are validated, but these checks do not establish semantic entailment; the reviewer inspects the generated interpretation. Exact citation validation and a final grant/catalog check precede protected audit persistence.

Response-cache keys bind the request, current principal, catalog revision and generation revision. A hit still receives current evidence validation, a fresh request ID and a new audit. Redis retrieval caching can wrap demo or governed production search. It stores signed identifiers that are rehydrated from canonical evidence; production cache identity binds the selected provider, catalog and retrieval configuration. Optional Redis quotas coordinate request admission across processes and fail closed when the shared store is unavailable.

The generator owns a cancellable HTTP client on a dedicated AnyIO loop while the workflow remains synchronous. One deadline covers its metadata checks and inference exchange; expiry cancels pending headers and body reads without closing the reusable client. Response bytes remain bounded. Disconnecting the client does not establish that the remote model immediately stops computation.

## Ingestion

Immutable source bytes enter a trusted store. Governed ingestion is an explicit opt-in against an existing PostgreSQL catalog and operator-initialized job schema. API and CLI share a context that binds the queue to the catalog ID and immutable authority UUID; demo retains its synthetic configuration. PostgreSQL owns idempotent jobs and fenced leases. Submission locks and rechecks current publisher grants before persisting intent. The digital parser runs in a restricted container; the parent verifies source metadata and hashes before transactional publication. Queue notifications wake workers while durable SQL job state remains authoritative. Canonical completion is followed by explicit authorized Weaviate synchronization; affected queries fail until current vector coverage is complete. See [governed ingestion](ingestion.md).

The optional native OCR worker accepts trusted operator inputs, verifies generation completion and stages extraction in quarantine. A scoped reviewer can approve, correct or reject the retained artifact. Publication checks both reviewer and submitter grants under database locks and commits the pages and job state together.

## Deployment boundaries

The free Hugging Face demo runs the Python workflow in a browser worker with public synthetic fixtures and session SQLite. Browser-side scope checks are demonstration behavior, not a confidentiality boundary against the visitor.

The server uses authenticated authority for protected data. Governed production requires an existing PostgreSQL catalog, Cognito identity configuration and an installed generation model pinned by tag and digest. Weaviate serving additionally requires pinned retrieval models and an authenticated HTTPS vector service. The operator indexes authorized borrower/date scopes after canonical publication; queries fail when their current scope is not fully indexed. Redis, monitoring and queue integrations have separate configuration. The Cortex adapter has a local contract harness; the browser does not call it. The AWS folder supplies operator-managed infrastructure configuration.

See [grounded synthesis](generation.md), [Weaviate setup](weaviate.md), [runtime contracts](LLD.md), [security](security.md), [recovery](recovery.md) and [deployment](deployment.md).
