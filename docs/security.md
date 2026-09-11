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
enforcement are subsequent work and not yet proven by this identity slice.

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
