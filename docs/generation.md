# Grounded synthesis

The authenticated server combines retrieved evidence and deterministic financial calculations with an Ollama-generated interpretation. Each generated statement carries citations and exact supporting quotes. The underwriter reviews that interpretation alongside the evidence and the server's DSCR assessment; generation cannot change calculated values, missing-document checks or the lending decision boundary.

## Configure an existing model service

Run an operator-managed Ollama service with an already installed local completion model. CreditLens does not download models or start, stop or modify the service. Inspect `/api/tags` and `/api/show` on that service to obtain the exact tagged name and its SHA-256 digest. A tag alone is mutable. Keep the selected model stable while requests run.

Set the following alongside the existing production identity, catalog and search configuration:

```text
CREDITLENS_GENERATION_URL=http://127.0.0.1:11434
CREDITLENS_GENERATION_MODEL=<installed-model:tag>
CREDITLENS_GENERATION_DIGEST=<64-lowercase-hex-digest-from-api-tags>
CREDITLENS_GENERATION_TIMEOUT_SECONDS=120
```

Both model and digest are required for a configured governed production server, including existing installations upgrading to this version. Demo mode remains extractive unless both are supplied. The public browser worker does not connect to Ollama.

The production borrower selector lists IDs from the authenticated subject's current SQL grants. Each ID is also its display label; industry is empty until a separate trusted borrower registry supplies business metadata. The endpoint rechecks the grant before returning the list. It does not import demo names or derive identity labels from generated text.

Plain HTTP is allowed only on a literal loopback address with no generation token. For an operator-managed remote endpoint, use HTTPS and set `CREDITLENS_GENERATION_TOKEN` through the deployment secret store. That endpoint must enforce the bearer credential and restrict access to authorized application hosts. Native local Ollama does not require authentication; supplying a client token alone does not secure a remotely exposed service. Keep certificate verification enabled. The client ignores proxy environment settings and rejects redirects. [Ollama authentication](https://docs.ollama.com/api/authentication)

Startup and readiness verify the configured digest and a local GGUF model with completion capability. Cloud-backed model descriptors are rejected. The service must expose compatible `/api/tags`, `/api/show` and `/api/chat` routes. Unavailable or changed models prevent successful readiness; there is no automatic model substitution.

## Request and support validation

The server first retrieves authorized canonical evidence, computes supported ratios using Decimal and determines whether the packet must abstain. An abstained packet skips generation entirely. Otherwise, the model receives the original question, effective date, selected evidence, exact computed values and the existing review state. It receives short evidence handles instead of authority to invent document identities.

The generator requests one non-streamed JSON-schema response, with temperature zero and thinking disabled for models that support it. The server validates the response independently; schema-constrained generation does not replace validation. [Ollama chat](https://docs.ollama.com/api/chat), [structured outputs](https://docs.ollama.com/capabilities/structured-outputs)

Each accepted statement selects supplied evidence handles or exact computed-metric names. The server attaches the original canonical text and citations, including every input source for a selected metric. Unknown or duplicate references are rejected. A selected metric's exact value must appear in the statement. Numeric tokens must occur in that statement's cited evidence, or match an exact server-calculated value with all of its input sources cited. Missing, malformed, foreign or unsupported output fails the request. A valid model refusal returns an explicit refusal category and a server-written reason without generated statements.

These checks establish source provenance and numeric presence. They do not prove that a source logically supports every generated claim. The attached quotes contain the full selected chunks, preserving original line breaks and wording. The UI labels the text **Generated interpretation**, places citations beside it and exposes those quotes for human review. Generated text and source text render as plain text.

Current grants and catalog revision are checked immediately before evidence is sent and again after generation. The model digest is also checked before and after inference. Final citation validation, current authority and a protected audit remain required before a response can succeed. Generated packets use `ollama-rag`; server-abstained packets use `rag-withheld` and have no synthesis. Existing packets without a synthesis field remain readable.

## Budgets and operation

Generation has one in-flight request per generator instance. Concurrent generation returns a busy error instead of an unbounded queue. The timeout is configurable from 1 to 180 seconds, with a 120-second default shared by model identity checks and inference. The client cancels pending response headers and body reads when that budget expires; a later request can reuse the client. Responses are limited to one megabyte and encoded responses are rejected. Network cancellation has normal scheduling and cleanup overhead and does not guarantee that the model service stops computing immediately.

The frontend query deadline is 240 seconds; borrower and source reads retain a 30-second deadline. Configure ingress and reverse-proxy timeouts to accommodate the query budget. Cancelling a browser request stops its response handling; it does not itself cancel server computation. Retrieval, SQL checks and audit persistence have their own limits outside the generation network budget.

The prompt payload is capped at 12,000 UTF-8 bytes. Fixed inference settings use a 16,384-token context, at most 2,048 output tokens and four CPU threads. The response body is bounded and must finish normally with the configured model identity. Partial or truncated output is rejected. Cold model loading shares the same request budget; size the installed model for the serving hardware and exercise a representative request before admitting traffic.

Model identity, prompt version and token counts accompany accepted synthesis. The response cache binds the generation model, digest, prompt, schema and fixed inference options; cache hits still require current authority, evidence validation and a new audit. Keep model upgrades deliberate and review their generated interpretations before rollout.

See [runtime contracts](LLD.md), [security](security.md) and [Weaviate setup](weaviate.md) for the surrounding server boundaries.
