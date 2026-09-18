# Runtime contracts

| Subsystem | Source | Contract |
| --- | --- | --- |
| Typed packets | [domain.py](../src/creditlens/domain.py) | Frozen validated request/response models; caller scope is not accepted as authority. |
| API and runtime | [api.py](../src/creditlens/api.py), [runtime.py](../src/creditlens/runtime.py) | Configure identity, catalog, provider, admission and workflow; expose health and readiness. |
| Identity and access | [auth.py](../src/creditlens/auth.py), [access.py](../src/creditlens/access.py) | Verify token contract and resolve current grants before evidence access. |
| Canonical storage | [sql_catalog.py](../src/creditlens/sql_catalog.py), [storage.py](../src/creditlens/storage.py) | Transactions publish evidence and revisions; audits are protected records. |
| Retrieval | [search_provider.py](../src/creditlens/search_provider.py), [hybrid_provider.py](../src/creditlens/hybrid_provider.py), [rerank_provider.py](../src/creditlens/rerank_provider.py) | Rank only scoped candidates, reject altered or foreign records, rehydrate canonical text. |
| Vector storage | [weaviate_provider.py](../src/creditlens/weaviate_provider.py), [weaviate_store.py](../src/creditlens/weaviate_store.py) | Scope IDs come from SQL; collection and model identity, complete scope coverage and canonical fingerprints are checked before ranking. |
| Local models | [llama_chunking.py](../src/creditlens/llama_chunking.py), [neural_search.py](../src/creditlens/neural_search.py) | Preserve exact source offsets and use the pinned model contract. |
| Packet construction | [workflow.py](../src/creditlens/workflow.py), [intent.py](../src/creditlens/intent.py) | Route reference and assessment requests, bound context, validate before audit and return. |
| Finance and citations | [finance.py](../src/creditlens/finance.py), [citations.py](../src/creditlens/citations.py) | Decimal arithmetic requires compatible inputs; citations resolve exact current source spans. |
| Cache and quotas | [response_cache.py](../src/creditlens/response_cache.py), [retrieval_cache.py](../src/creditlens/retrieval_cache.py), [limits.py](../src/creditlens/limits.py) | Cache hits repeat authority checks; shared quota errors prevent execution. |
| Durable ingestion | [ingestion_jobs.py](../src/creditlens/ingestion_jobs.py), [ingestion_worker.py](../src/creditlens/ingestion_worker.py) | Lease fencing and grant locks protect transactional publication. |
| Source and OCR | [source_store.py](../src/creditlens/source_store.py), [ocr.py](../src/creditlens/ocr.py) | Hash and containment checks precede processing; OCR admission requires review. |
| Queue | [sqs_queue.py](../src/creditlens/sqs_queue.py), [worker_loop.py](../src/creditlens/worker_loop.py) | Loopback notification transport, bounded retry and SQL polling; leases own completion. |
| Operations | [observability.py](../src/creditlens/observability.py), [ingestion_metrics.py](../src/creditlens/ingestion_metrics.py) | Allowlisted trace fields, bounded labels and protected monitoring access. |

## Failure behavior

A changed grant or catalog invalidates stale evidence. Failed citation validation or audit persistence prevents a successful packet. Redis retrieval-cache failure can recompute authorized work; shared-quota failure cannot grant a local replacement allowance. Required search/scorer failures fail the request. Missing financial inputs produce an explicit incomplete packet.

A stale worker cannot complete another lease. Failed publication rolls back canonical changes and job completion. Review rejection publishes nothing. Duplicate queue messages do not override SQL job state. After restoration, reconcile current revocations before reopening access.

## Workbench

The browser validates wire contracts, preserves Decimal values as strings and renders sources as literal text. Borrower/date changes cancel pending work and clear completed packets. Source reads use the completed packet's scope. The browser worker serializes JSON explicitly across the Python bridge.

Use generated OpenAPI at `/docs` for route schemas and [.env.example](../.env.example) for configuration.
