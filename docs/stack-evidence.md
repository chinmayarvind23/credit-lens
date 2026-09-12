# Original stack and verification scope

| Technology | Implemented role | Verification boundary |
| --- | --- | --- |
| Python / FastAPI | Typed evidence workflow, Decimal finance, query/source API, current grants and audits | Local HTTP, security, financial and concurrency tests |
| LlamaIndex | Exact-span sentence parsing and optional retrieval experiments | Local parser/retrieval checks; the free browser uses lexical retrieval |
| Snowflake Cortex | Filtered REST search adapter and governed PostgreSQL runtime composition | Adapter contracts plus signed-token API/real SQL tests with simulated Cortex responses; no live account verification claimed |
| Redis | Signed retrieval-cache entries with current scope and canonical revalidation | Retained local Redis integration tests; distinct from the process-local response cache |
| OpenSearch | Real filtered lexical adapter and controlled local indexing experiment | Retained real local service checks; no managed AWS OpenSearch deployment claimed |
| AWS | Terraform deployment infrastructure, IAM policies and operator instructions for us-east-1 | Infrastructure validation/reference only; resources were not provisioned under the no-spending instruction |
| DeepEval / RAGAS | Local judge runners, controls, saved-packet exports and raw-result verification | RAGAS population cited-field run completed; whole-packet DeepEval diagnostic was stopped when the user prioritized résumé delivery |
| LangSmith / OTel tracing role | OpenTelemetry SDK traces and Prometheus metrics | Real local trace/metric and HTTP checks; LangSmith hosting is not claimed |

Implementation references: [Cortex runtime](../infra/cortex/README.md),
[OpenSearch](../infra/opensearch/README.md), [Redis](../infra/redis/README.md),
[AWS](../infra/aws/README.md), [evaluation](evaluation.md),
[observability](observability.md), [retrieval experiments](../infra/retrieval/README.md).

The strongest retained résumé measurements are 3,840 synthetic pages, 240 authored
adversarial questions and Recall@10 improvement from 74.05% to 86.54% across 220
eligible questions. These are local development results, not production lender
accuracy. The free interactive deployment and demo recording are delivered.
Unsupported accuracy, cloud-cost and stable production-speedup claims are omitted.
