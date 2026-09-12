# Read-only GraphQL admin inspection

The optional explorer reads current canonical catalog counts, scoped ingestion-job
status and the administrator's own audit metadata. It is implemented inside the
existing server and as a trusted operator CLI. It does not return document text,
protected query/packet JSON, other subjects' audit history or worker lease tokens.

Install `uv sync --locked --extra admin` and set
`CREDITLENS_GRAPHQL_ENABLED=true`. Preserve other extras you use when syncing.
The ordinary public demo identity remains an underwriter and receives 403.
The existing Docker demo does not install this optional extra.

## Local operator

The local CLI is usable with an existing administrator grant in the configured
SQL database. It does not create or promote identities. Save a query as
`inspect.graphql`:

```graphql
query Inspect {
  viewer { subject tenantId grantRevision }
  catalog(borrowerId: "borrower-001", effectiveAt: "2026-09-11") {
    revision documentCount pageCount chunkCount
  }
  recentAudits(borrowerId: "borrower-001", first: 10) {
    requestId createdAt disposition searchProvider
  }
}
```

Run `python -m scripts.inspect_admin --subject <existing-admin> --query inspect.graphql`.
Optional `--variables variables.json` and `--operation Inspect` use standard GraphQL
variables and operation selection. The CLI trusts local operator access to the SQL
configuration; it is not a network authentication endpoint. Default local execution
uses the existing synthetic catalog. Production workflow integration remains unfinished.

## HTTP contract

POST `/api/v1/admin/graphql` with the existing Bearer access-token contract and JSON:

```json
{"query":"query Job($id: ID!){ingestionJob(id:$id){jobId state attempts errorCode}}","variables":{"id":"<job-uuid>"},"operationName":"Job"}
```

The current SQL grant must be administrator-scoped. Token roles do not replace
current grants. HTTP tests exercise actual RSA signatures with a synthetic issuer,
current grant changes and real PostgreSQL job reads; no live managed issuer is
claimed. Production HTTP use also requires the configured serving workflow to be
available. The local operator path supports inspection without relaxing the public
demo's identity boundary.

Aliases, variables and acyclic fragments are supported. Standard GraphQL validation
runs before resolvers. The endpoint bounds document tokens, expanded selections,
root fields, depth and audit rows. It is query-only, with introspection disabled.
Any resolver failure discards partial data. Final grant and catalog checks reject
results if authority changed during execution. Responses retain the API's no-store
policy and error messages do not reflect query text or raw database errors.

GraphQL-core's [execution](https://graphql-core-3.readthedocs.io/en/stable/usage/queries.html)
and [validation contracts](https://graphql-core-3.readthedocs.io/en/stable/modules/validation.html)
provide parsing, schema checks and synchronous resolution. This application's
permission checks and all-or-error response policy are additional constraints.

This is an operational metadata explorer, not a new semantic grading system.
Whole-answer evaluation results remain governed by the separate benchmark ledger.
