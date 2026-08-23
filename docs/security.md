# CreditLens Security Design

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
