# Security

The authenticated server resolves current SQL grants after verifying token issuer, signature, audience and token-use requirements. Request text, role claims and document content cannot expand authority. Every query and source read applies tenant, borrower, access-group and policy-date restrictions.

Search indexes are candidate providers. The canonical catalog supplies current text, revisions and permissions. Citation validation compares exact spans against authorized canonical evidence. Cache keys bind scope and revision; cache hits repeat validation and create a fresh protected audit. The final authority check rejects grant changes during execution.

The browser demo contains public fictional documents. Its client-side checks illustrate the workflow; they do not protect confidential data from the visitor. Do not bundle private documents or credentials into a public build.

Digital ingestion validates immutable source hashes and metadata and executes parsing in a restricted container. Native OCR is an opt-in trusted-operator path and stages extraction for scoped review. Approval locks current reviewer and submitter grants through publication. Untrusted public uploads need a deployment with an appropriate execution boundary.

Logs and telemetry exclude raw questions, document text, credentials and identity labels. SQL audits remain protected application data. Administrative inspection rechecks current privileges and does not expose source text or worker lease tokens.

Shared quotas use atomic Redis admission with bounded keys and expiry. Configure consistent namespaces and limits across workers and a noeviction policy. Quota-store failure fails closed; response/retrieval caches do not authorize a request.

Keep secrets in local environment configuration or the operator's secret store. [.env.example](../.env.example) contains configuration names, not production credentials. See [recovery](recovery.md) for revocation reconciliation after a restore.
