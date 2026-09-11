# CreditLens Security Design

## Implemented identity boundary

`src/creditlens/auth.py` validates Cognito access JWTs with RS256, configured
issuer, expiry, issue time, token_use, client_id and required scope. JWK selection
uses the configured issuer endpoint. Subject lookup reads the current SQL grant
for every request; caller-supplied tenant and groups never establish permission.
Disabled or unknown subjects receive the same denial. Tests use real RSA keys
and local JWK fixtures. Live Cognito login is not yet verified.

Demo mode grants a fixed public synthetic identity access to five synthetic
borrowers. Startup rejects Cortex credentials in demo mode. Production requires
Cognito and Cortex settings and never seeds a demo grant. Local SQLite metadata
is supported; shared Postgres deployment remains to be verified.

Every factual output contract carries citations, including summaries and metric
inputs. Query models reject extra fields. Canonical pages use half-open policy
date windows. These are implemented contracts; retrieval and generated-output
enforcement for live model and provider integrations remains unverified.

## Implemented local query controls

The local evidence catalog filters tenant, borrower, ACL groups, extraction
confidence and effective date before ranking. Revoking a page revokes every
chunk on that page and invalidates in-flight snapshots. Query completion checks
the catalog revision and current SQL grant again. The source drawer separately
authorizes each fetch. API responses include Cache-Control: no-store.

Streamed JSON bodies are capped at 16 KiB with a ten-second read deadline. The
local query limiter permits 60 requests per minute per authenticated subject,
per process. Shared deployment quotas remain a gateway/Redis requirement.

The SQL audit store preserves the protected query and packet, their SHA-256
hashes, corpus version, grant/catalog revisions and execution stage names before
acknowledging success. This is sensitive application storage, not general-purpose
telemetry. The local database is ignored by Git and only the synthetic demo uses
it. Production requires protected database access, encryption, retention and
restore verification; that deployment has not been completed. Ordinary traces
must never copy protected_packet or protected_query from audit records.
The packet hash scope is `packet-before-runtime-timings-v1`: it covers the
acknowledged business payload before response stage timings and total latency
are added. It does not claim to hash the exact final HTTP bytes.

The demo serves authored synthetic pages in memory. The physical PDF extraction
and its metrics are a separate reproducible path. Public document upload is not
enabled because untrusted PDF process isolation and OCR remain incomplete.

## Objective

Unauthorized evidence must never enter retrieval or model context.

`UnauthorizedRetrievedChunks = 0`

## Authentication

Default: AWS Cognito with OIDC/JWT.

Backend validates issuer, audience, expiration, signature, and required claims.

Do not implement password storage.

## Authorization

Resolve server-side:

- tenant ID,
- user ID,
- role,
- borrower scope,
- ACL groups.

Never accept these as model-generated arguments.

## Retrieval filter order

Required:

`auth -> ACL resolution -> metadata filter -> retrieval -> rerank`

Filtering after broad retrieval is prohibited.

## Threat model

Cover:

- cross-tenant retrieval,
- wrong borrower access,
- stale/revoked permissions,
- IDOR,
- prompt injection in uploaded documents,
- malicious PDFs,
- SQL injection,
- oversized uploads,
- secret exposure,
- sensitive data in logs,
- cache isolation bugs,
- stale-policy selection,
- corrupted OCR,
- denial of service.

## Security tests

Include same-tenant allowed borrower, same-tenant forbidden borrower, different tenant, expired token, tampered borrower ID, modified ACL claims, prompt injection in PDF, malformed PDF, oversized upload, SQL injection attempt, and cache-isolation attempt.

## Logging

General telemetry contains IDs and metadata, not raw borrower PII or full document text.

## Secrets

Use `.env`, AWS secret management where appropriate, GitHub secret scanning, and no committed credentials.

Changes touching auth, ACL, cache keys, retrieval filters, document ownership, policy versioning, or SQL construction require focused security tests and line-by-line review.
