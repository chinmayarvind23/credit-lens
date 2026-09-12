# Cortex Search provider contract

The optional `CortexSearchProvider` implements scoped REST retrieval. The caller owns the HTTP client lifecycle. See [runtime setup](../infra/cortex/README.md).

The caller supplies a trusted principal and `QueryRequest`. The provider resolves
current SQL grants, snapshots canonical allowed chunks, and builds the entire filter
on the server. The filter combines tenant, borrower or shared policy, ACL overlap,
half-open effective dates and exact currently allowed chunk IDs. It never accepts
client filter expressions. This is required because Cortex Search uses owner rights;
access to its service can expose indexed rows independent of underlying table row
policies. [Snowflake query guide](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-search/query-cortex-search-service).

`index_record(chunk)` defines the application index projection. Ingestion must add
the chunk's canonical text as a separate `SEARCH_TEXT` column. The service searches
that column; the adapter requests only provenance columns. These must be configured
as filterable attributes:

| Column | Snowflake source type | Contract |
| --- | --- | --- |
| CHUNK_ID | VARCHAR | Immutable canonical chunk ID |
| TENANT_ID | VARCHAR | Server-owned tenant |
| BORROWER_SCOPE | VARCHAR | Borrower ID, or `:shared-policy:` for shared policy |
| ACL_GROUPS | ARRAY | Ordered nonempty array of group strings |
| VALID_FROM | DATE | Inclusive start |
| VALID_TO_EXCLUSIVE | DATE | Exclusive end; open end encoded as `9999-12-31` |

The source query also projects DOCUMENT_ID, DOCUMENT_VERSION, PAGE, CONTENT_HASH,
START_CHAR, END_CHAR and RECORD_SHA256. PAGE and offsets serialize as JSON integers;
dates serialize as ISO date strings. RECORD_SHA256 hashes the complete canonical
chunk JSON, including text and parser/chunker provenance, with sorted keys and compact
separators. The terminal date `9999-12-31` is unsupported as a query date. The reserved
shared scope cannot collide with the public borrower ID grammar.

Every response row must contain exactly the requested projection and match its
canonical record, including types, dates, ACL order and digest. An unknown, duplicate,
revoked, malformed or mismatched row rejects the whole ranking. Only local canonical
text can reach a citation. The response envelope may include provider request
metadata. [REST API reference](https://docs.snowflake.com/en/developer-guide/snowflake-rest-api/reference/cortex-search-service).

`search()` rechecks grants and catalog revision after HTTP. Consumers must call
`verify(result)` before publishing downstream output. `citation(result, citation)`
checks exact provenance and current grants/catalog again. A source drawer should
perform a new authorized lookup on each access. Catalog revisions detect stale snapshots. Use the PostgreSQL canonical catalog when coordinating server instances.

Limits are 100 returned chunks, 1,024 allowed candidate IDs, one request, 1 MiB response
bytes, and a socket timeout in `(0, 30]` seconds. Large allowed scopes fail closed; the
adapter does not partition rankings or remove filters. There is no retry or lexical
fallback. HTTPS endpoints are restricted to Snowflake account hosts with conventional
unquoted database/schema/service identifiers. Redirects and compressed responses are
rejected. Secrets remain in authorization headers and curated errors omit provider
bodies. Socket timeouts are not a hard end-to-end deadline against a slow streaming
peer.
