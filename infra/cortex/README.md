# Governed Snowflake Cortex runtime

The production API can compose the implemented Cortex Search REST adapter with an
existing PostgreSQL canonical catalog. Cognito verifies identity, current SQL
grants authorize each query, and Cortex ranks only the current authorized candidate
IDs. Returned metadata must match canonical records exactly. Finance and packet
assembly remain deterministic/extractive; the audit records
`search_provider_mode=snowflake-cortex-rest` separately from packet generation mode.

An operator must supply an existing catalog and grant database, indexed Cortex
service and Cognito pool. These managed resources have not been provisioned or
verified live by this project. Configure credentials locally, never in source:

```text
CREDITLENS_MODE=production
CREDITLENS_DATABASE_URL=postgresql+psycopg://<operator-managed-connection>
CREDITLENS_CATALOG_BACKEND=postgres
CREDITLENS_GOVERNED_CATALOG_ID=<existing-catalog-id>
CREDITLENS_ISSUER=https://cognito-idp.us-east-1.amazonaws.com/<pool>
CREDITLENS_CLIENT_ID=<app-client-id>
CREDITLENS_CORTEX_URL=https://<account>.snowflakecomputing.com/api/v2/databases/<DB>/schemas/<SCHEMA>/cortex-search-services/<SERVICE>:query
CREDITLENS_CORTEX_TOKEN=<operator-managed-token>
```

Run the normal FastAPI factory from the project README. The configured catalog
must already exist; production startup never seeds synthetic pages or creates a
catalog. An absent governed catalog ID retains the previous unavailable-workflow
behavior. An invalid or nonexistent configured catalog fails startup. Required
Snowflake index columns and provenance values come from
`creditlens.cortex_search.index_record`; filters use the adapter's tenant, borrower,
ACL, effective-date and exact chunk-ID attributes. Index creation/population and
live account permissions remain operator responsibilities.

The HTTP pool disables proxies and redirects and uses finite timeouts. Search
failures do not trigger an unfiltered/local fallback. Optional response caching
still rechecks current authority and canonical evidence and writes a fresh audit.
The synthetic ingestion endpoints remain demo-only; this change does not introduce
production document mutation APIs.

Local verification uses actual PostgreSQL, RSA-signed access tokens, API requests,
canonical source revocation, warm-cache grant revocation and persisted audits.
Only Cortex's remote REST response is simulated, explicitly. The test also checks
that startup leaves the catalog revision unchanged and closes the provider client.
Run it with the owned PostgreSQL fixture documented in [the PostgreSQL guide](../postgres/README.md):

```powershell
uv run --no-sync pytest infra/postgres/test_catalog.py tests/test_cortex_search.py
```

No live Snowflake search, managed Cognito deployment, cloud latency or service cost
is claimed by this local verification. The free Hugging Face browser demo continues
to use local lexical retrieval and needs none of these services.
