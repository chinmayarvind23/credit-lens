# CreditLens project summary

CreditLens is a working commercial-lending evidence prototype with a free interactive
[Hugging Face demo](https://huggingface.co/spaces/chinmayarvind/creditlens).
The server enforces current permissions; the browser demonstrates the workflow over
public fictional documents. The underwriter retains every lending decision.

## Project bullets

- Built a permission-aware lending RAG system over **3,840 synthetic policy and borrower-document pages**, using LlamaIndex, hybrid retrieval, selective query grounding, reranking and page-level citations to reach **86.54% Recall@10** and **0.788 nDCG@10**, improving recall by **12.49 percentage points** over lexical search.
- Created a **240-question adversarial evaluation suite** covering policy interpretation, borrower evidence, missing documents, contradictions, restricted data and exceptions; integrated **DeepEval/RAGAS**, verified **240/240 deterministic workflow checks**, and completed **235 whole-answer judgments plus eight controls** with retained raw evidence.
- Added Redis caching and shared quotas, current SQL access controls, OpenTelemetry tracing and CI quality/latency checks; measured **300 ms p95 at eight concurrent clients** across two local API processes with **360 distinct PostgreSQL audits**, and deployed a free interactive Hugging Face demo with optional AWS Terraform setup.

The retrieval numbers describe the local hybrid experiment on 220 eligible authored
questions. The latency number describes a separate local lexical workload with warm
response caching. Deterministic checks are not a semantic-accuracy claim. The published
browser uses lexical retrieval; the neural experiment runs locally.

## Where the stack belongs

| Stack | Concrete responsibility |
| --- | --- |
| Python / FastAPI / SQLAlchemy | Typed query API, deterministic Decimal calculations, grants and protected audits |
| LlamaIndex / SentenceTransformers | Source-span chunking, optional embeddings and cross-encoder reranking |
| Snowflake Cortex / OpenSearch | Configurable scoped search adapters; Cortex contract-tested, OpenSearch also exercised against a local service |
| Redis / PostgreSQL | Retrieval cache, shared request quotas, canonical evidence, worker state and durable audits |
| DeepEval / RAGAS | Whole-answer and cited-field evaluation, frozen controls, raw journals and reconciliation |
| OpenTelemetry / Prometheus / Grafana | Request traces, model-stage metrics, cost-known flags and 38 dashboard panels |
| TypeScript / Bun / Pyodide / Hugging Face | Interactive browser workbench and free hosting of the public synthetic workflow |
| AWS / Terraform | Optional deployment configuration and operator setup; no AWS provisioning or spend |

Expanded engineering work includes gRPC transport, GraphQL administration, SQS-compatible
notifications, reviewed OCR ingestion, FAISS/Weaviate comparisons, a Spark backfill benchmark
and an optional Supabase public directory. Each has a specific role and recorded validation;
see the complete [stack map](stack-evidence.md) for implementation and deployment boundaries.
OpenTelemetry supplies tracing; a hosted LangSmith integration is not claimed.

## Inspect the evidence

- [Architecture](HLD.md) and [implementation details](LLD.md)
- [Evaluation definitions and results](evaluation.md)
- [Deployment and latest verification](delivery.md)
- [Stack roles and reproduction links](stack-evidence.md)
- [Recovery](recovery.md) and [monitoring](../infra/monitoring/README.md)

Latest verification: **567 tests passed**, five optional integration tests skipped,
**240/240 deterministic cases passed**, and the three identified refusal failures improved
from **0/3 to 3/3** in a new actual DeepEval diagnostic with both controls correct.
The historical full DeepEval result remains **102/235**; a separate agent review passed
27/30 sampled historical packets. Neither is independent human calibration or a validated
production accuracy rate. No unmeasured citation precision, cost savings or lender impact
is claimed.
