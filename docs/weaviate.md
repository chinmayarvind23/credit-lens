# Weaviate vector search

The authenticated server can use persistent Weaviate vectors together with lexical search and local reranking. PostgreSQL owns document text, permissions and revisions. Weaviate stores vectors and canonical record fingerprints; returned candidates must match the current canonical records before they enter a packet.

## Configure the server

Provide an existing governed PostgreSQL catalog and current administrator and user grants. Configure a Cognito user pool and app client for the server's access-token contract. Provision a persistent Weaviate service with HTTPS, API-token authentication, restricted network access and operator-managed backups. The disposable [local test container](../infra/weaviate/README.md) has different security and storage settings.

Install the locked retrieval dependencies with `uv sync --locked --extra retrieval`, retaining any other extras the deployment uses. Prepare the pinned embedding and reranker files following [local model setup](../infra/retrieval/README.md). Keep that model directory read-only while serving.

Set these values in the process environment or deployment secret store:

```text
CREDITLENS_MODE=production
CREDITLENS_PRODUCTION_SEARCH=weaviate
CREDITLENS_DATABASE_URL=postgresql+psycopg://<operator-managed-connection>
CREDITLENS_CATALOG_BACKEND=postgres
CREDITLENS_GOVERNED_CATALOG_ID=<existing-catalog-id>
CREDITLENS_ISSUER=https://cognito-idp.<region>.amazonaws.com/<pool>
CREDITLENS_CLIENT_ID=<app-client-id>
CREDITLENS_REQUIRED_SCOPE=creditlens/query
CREDITLENS_WEAVIATE_URL=https://<vector-service-host>
CREDITLENS_WEAVIATE_TOKEN=<operator-managed-api-token>
CREDITLENS_WEAVIATE_COLLECTION=CreditLensEvidence
CREDITLENS_LOCAL_MODEL_DIRECTORY=<absolute-pinned-model-directory>
```

Leave `CREDITLENS_RETRIEVAL_MODE` at its default `lexical`; that setting selects the demo's local retrieval mode. The production selector above assembles the Weaviate hybrid workflow. Remove Cortex URL and token settings when selecting Weaviate. Supply secrets through the environment or secret store, never shell arguments or source files.

## Optional self-hosted vector service

[compose.production.yaml](../infra/weaviate/compose.production.yaml) runs a single Weaviate node with a pinned image, native HTTPS and the named `evidence_vectors` volume. It exposes only `127.0.0.1:18443` for a server on the same host. Anonymous access is disabled. The `indexer` identity can manage collections and write vectors; the `reader` identity has read-only access under Weaviate's [Admin list authorization](https://docs.weaviate.io/deploy/configuration/authorization). These service identities are separate from CreditLens's current SQL grants.

Prepare an external TLS directory containing `server.crt` and `server.key`. Use a certificate whose hostname matches the configured service URL and whose chain the Python HTTP client trusts. The certificate directory is mounted read-only. Keep certificate verification enabled; an untrusted self-signed certificate is unsuitable for the normal application configuration.

Load separate indexer and reader keys from the deployment secret store into `CREDITLENS_WEAVIATE_ADMIN_KEY` and `CREDITLENS_WEAVIATE_READER_KEY`. Set `CREDITLENS_WEAVIATE_TLS_DIR` to the absolute certificate directory. Keep these keys and private certificate material outside the repository. After making the exact pinned image available locally, run:

```powershell
docker compose -f infra/weaviate/compose.production.yaml up -d
```

Set `CREDITLENS_WEAVIATE_URL` to `https://<certificate-hostname>:18443`, with that hostname resolving to loopback on the application host. In the indexing process, set `CREDITLENS_WEAVIATE_TOKEN` to the indexer key. In the serving process, set it to the reader key. The API process needs no indexer key. Use separate process environments so writer credentials do not carry into serving.

The volume survives ordinary container replacement and `docker compose down`. Stop this stack without removing its data:

```powershell
docker compose -f infra/weaviate/compose.production.yaml down
```

Do not add `--volumes` when retaining the index. This is a single-node deployment: host or disk failure interrupts vector search. The restart policy handles container restarts; it does not provide replication or an independent backup.

Keep query ingress closed and stop the vector service before taking a complete offline volume snapshot with the host's storage tooling. Store the snapshot away from that host, along with the image digest, collection/model contract and restore instructions. Restore into a fresh isolated volume under the same pinned configuration. Restore TLS material and credentials through their separate secret-management procedures. Reconcile SQL authority, synchronize current scopes and exercise readiness, allowed queries and denied access before reopening traffic. The vector index can also be rebuilt from the governed catalog and pinned models. For online backups, configure a supported backup module and off-host destination following [Weaviate backup guidance](https://docs.weaviate.io/deploy/configuration/backups); this Compose file leaves that policy to the operator.

## Populate the index

From the repository root, create the collection explicitly and synchronize a scope using an existing SQL administrator subject:

```powershell
uv run --no-sync python scripts/index_weaviate.py --subject ADMIN --borrower ID --effective-at YYYY-MM-DD --create-collection
```

Replace `ADMIN`, `ID` and `YYYY-MM-DD` with the administrator subject, authorized borrower and relevant policy date. Use `--create-collection` only for a new collection. Repeat without that flag for each required borrower/date scope and after publishing canonical changes:

```powershell
uv run --no-sync python scripts/index_weaviate.py --subject ADMIN --borrower ID --effective-at YYYY-MM-DD
uv run --no-sync uvicorn creditlens.api:create_app --factory --host 127.0.0.1 --port 8000
```

The command resolves current SQL grants and embeds only that administrator's authorized canonical snapshot. It rechecks grants after embedding and before writing each batch. Repeated synchronization replaces the same deterministic vector identities. SQL publication and vector synchronization are separate operations. Every request checks complete index coverage for its current authorized scope; missing or stale entries fail the request until synchronization completes. Startup checks the collection and model contract; `/ready` also probes authenticated vector-query access.

## Request and recovery behavior

SQL resolves the current tenant, borrower, access groups and effective date before search. A single vector query filters against those authorized identities and requests the entire bounded scope in vector order. The client verifies that the returned fingerprint set exactly matches the canonical allowlist before taking the highest-ranked candidates. This also detects embeddings that are visible as stored objects but not yet searchable. Lexical and vector ranks are fused, the pinned cross-encoder reranks them, and query grounding precedes packet construction. Each request admits at most 1,000 authorized chunks; the full-scope completeness check bounds this serving path to that workload. Both branches are required; an oversized scope or a search/model failure returns an error. Citation checks, permission rechecks and protected audit writes remain in the ordinary workflow.

A restored or changed vector index must match the configured catalog and model identity. Rebuild into a new collection when changing that contract, synchronize every required scope, and update the server configuration together. Keep the prior collection available for a deliberate rollback. Follow [canonical recovery](recovery.md) to reconcile grants and revocations before restoring access. Revoked records may remain in vector storage, but current SQL scope excludes them from user queries; storage retention and deletion remain operator responsibilities.

The public browser demo continues to use synthetic evidence and local lexical retrieval. The AWS demo reference packages that synthetic application; provision protected server infrastructure separately.
