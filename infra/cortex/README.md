# Governed Snowflake Cortex runtime

The production API can compose the implemented Cortex Search REST adapter with an
existing PostgreSQL canonical catalog. Cognito verifies identity, current SQL
grants authorize each query, and Cortex ranks only the current authorized candidate
IDs. Returned metadata must match canonical records exactly. Finance and packet
assembly remain deterministic/extractive; the audit records
`search_provider_mode=snowflake-cortex-rest` separately from packet generation mode.

An operator must supply an existing catalog and grant database, indexed Cortex
service and Cognito pool. This setup requires operator-managed resources. Configure credentials locally, never in source:

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

```powershell
uv run --no-sync pytest infra/postgres/test_catalog.py tests/test_cortex_search.py
```

The Hugging Face browser runtime uses local lexical retrieval and does not require these services.
