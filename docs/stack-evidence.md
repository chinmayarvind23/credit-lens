# Original stack and verification scope

| Technology | Implemented role | Verification boundary |
| --- | --- | --- |
| Python / FastAPI | Typed evidence workflow, Decimal finance, query/source API, current grants and audits | Local HTTP, security, financial and concurrency tests |
| LlamaIndex | Exact-span sentence parsing and optional retrieval experiments | Local parser/retrieval checks; the free browser uses lexical retrieval |
| Snowflake Cortex | Filtered REST search adapter and governed PostgreSQL runtime composition | Adapter contracts plus signed-token API/real SQL tests with simulated Cortex responses; no live account verification claimed |
| Redis | Signed retrieval-cache entries plus atomic shared per-identity quotas with finite TTL | Actual local cache tests and 24 targeted quota/workflow/RPC tests; quota outage fails closed; response cache remains process-local |
| OpenSearch | Real filtered lexical adapter and controlled local indexing experiment | Retained real local service checks; no managed AWS OpenSearch deployment claimed |
| AWS | Terraform deployment infrastructure, IAM policies and operator instructions for us-east-1 | Infrastructure validation/reference only; resources were not provisioned under the no-spending instruction |
| DeepEval / RAGAS | Local judge runners, controls, saved-packet exports and raw reconciliation | Both populations reconciled; GEval v2 102/235 raw passes (43.4%), 8/8 controls, no human calibration or validated project-quality claim |
| OTel / Prometheus / Grafana | Local traces, finite operational/cost metrics and provisioned operational/evaluation/indexing dashboards | 23 operational + six evaluation + nine indexing panels; measured snapshots remain distinct from validated quality; no hosted LangSmith |
| PostgreSQL | Canonical authority, durable jobs and protected audits | Actual shared-state and one backup/restore drill preserving 330 canonical rows, audits and saved revocations; later revocations need reconciliation |
| FAISS / Weaviate | Exact-set comparison and four HNSW configurations | Retained actual neighbor IDs and scope checks; Weaviate not promoted |
| Spark | Local metadata backfill compared with serial Python | Equivalent normal/tenfold-replay output; Python faster at this size |
| gRPC / GraphQL | Versioned packet transport and bounded read-only admin explorer | Actual local sockets/signed-token HTTP; no remote production service |
| Supabase | Optional public fictional borrower directory | Local SQL RLS/write-denial and browser fallback tests; live release uses bundled labels |
| PaddleOCR-VL | Opt-in trusted native OCR worker, observed EOS and durable review | Actual financial fixture and quarantine; no public-file sandbox or automatic approval |

Implementation references: [Cortex runtime](../infra/cortex/README.md),
[OpenSearch](../infra/opensearch/README.md), [Redis](../infra/redis/README.md),
[AWS](../infra/aws/README.md), [evaluation](evaluation.md), [observability](observability.md),
[retrieval experiments](../infra/retrieval/README.md).

The strongest retained résumé measurements are 3,840 synthetic pages, 240 authored adversarial
questions and Recall@10 improvement from 74.05% to 86.54% across 220 eligible questions. These are
local development results, not production lender accuracy. The free interactive deployment and demo
recording are delivered. Unsupported accuracy, cloud-cost and stable production-speedup claims are
omitted.

Runtime dollar-cost metrics separate known from unknown and never count unknown values as zero.
Actual RAGAS journals report 538,444 prompt/65,932 completion tokens. OpenSearch indexing
acknowledged 3,840 chunks in 0.957 seconds, with one canary at 0.990 seconds and one separate
deliberately rejected write. These are snapshot observations, not continuous production freshness,
natural model-error rates or cloud cost. See [monitoring](../infra/monitoring/README.md) and
[recovery](recovery.md).

Later checks measured actual neural-operation counts and injected-failure recovery, plus complete
SQL-publication-to-search visibility for all 3,840 chunks. Two recorded local visibility runs took
4.890 and 2.910 seconds after commit return. See
[publication visibility](../infra/monitoring/publication-visibility.md) and
[neural monitoring](../infra/monitoring/neural.md) for scope and reproduction.
