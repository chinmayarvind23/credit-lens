# Original stack and verification scope

| Technology | Implemented role | Verification boundary |
| --- | --- | --- |
| Python / FastAPI | Typed evidence workflow, Decimal finance, query/source API, current grants and audits | Local HTTP, security, financial and concurrency tests |
| TypeScript / Bun / Pyodide / Hugging Face | Built workbench and Python workflow in a browser worker, published on free HF Static hosting | Recorded public/browser/offline checks; no private-data security boundary in distributed browser assets; historical tunnel instructions are not the current release path |
| LlamaIndex | Exact-span sentence parsing and optional retrieval experiments | Local parser/chunking checks; no claim that LlamaIndex orchestrates every production request |
| SentenceTransformers / NumPy | Optional pinned MiniLM embedding, exact dense ranking and cross-encoder reranking | Actual local CPU model/HTTP experiments and operation metrics; public browser remains lexical, with no remote model inference |
| Snowflake Cortex | Filtered REST search adapter and governed PostgreSQL runtime composition | Adapter contracts plus signed-token API/real SQL tests with simulated Cortex responses; no live account verification claimed |
| Cognito-compatible OIDC / JWT | Issuer-pinned RS256 access-token validation plus current SQL grants | Signed-token contracts; no live Cognito pool, managed login UI or AWS identity deployment verified |
| Redis | Signed retrieval-cache entries plus atomic shared per-identity quotas with finite TTL | Actual local cache tests and 24 targeted quota/workflow/RPC tests; quota outage fails closed; response cache remains process-local |
| OpenSearch | Real filtered lexical adapter, RRF provider composition and local indexing/visibility experiments | Actual local search, exact metadata/text and revocation checks; complete SQL publication-to-search fixture covers all 3,840 chunks; no managed AWS deployment |
| AWS / Terraform | Optional operator reference for ECS/Fargate, ECR, ALB, CloudFront and CloudWatch Logs in us-east-1 | Validated configuration and mocked plans only; AWS is outside current delivery scope by user directive; no account-backed plan, apply or deployed resource claim |
| DeepEval / RAGAS | Local judge runners, controls, saved-packet exports and raw reconciliation | Both populations reconciled; GEval v2 102/235 raw passes (43.4%), 8/8 controls, no human calibration or validated project-quality claim |
| OTel / Prometheus / Grafana | Local SDK traces, finite operational/cost/neural metrics and provisioned operational/evaluation/indexing dashboards | 23 operational + six evaluation + nine indexing panels; imported evaluation and indexing snapshots are distinct from live request metrics and validated quality |
| LangSmith | Retained observability design option | No implemented LangSmith exporter, hosted trace integration or subscription is claimed; local tracing uses OTel |
| PostgreSQL | Canonical authority, durable jobs and protected audits | Actual shared-state and one backup/restore drill preserving 330 canonical rows, audits and saved revocations; later revocations need reconciliation |
| SQLAlchemy / SQLite | Database access and lightweight local/session grants and audits | SQLite demo behavior is separate from PostgreSQL cross-process durability; the browser's session audit is not a protected central audit service |
| boto3 / SQS / ElasticMQ | Job-ID notification adapter, redelivery, deletion and worker recovery over SQL-owned jobs | Actual local ElasticMQ send/receive/visibility/dead-letter checks; adapter explicitly targets the loopback fixture, not live AWS SQS |
| pypdf / ReportLab / Poppler / Docker | Physical synthetic PDF generation, extraction and isolated digital parsing/rendering | Actual PDF/page checks and bounded local parser containers; public untrusted OCR sandbox readiness is not claimed |
| FAISS / Weaviate | Exact-set comparison and four HNSW configurations | Retained actual neighbor IDs and scope checks; Weaviate not promoted |
| Spark | Local metadata backfill compared with serial Python | Equivalent normal/tenfold-replay output; Python faster at this size |
| gRPC / GraphQL | Versioned packet transport and bounded read-only admin explorer | Actual local sockets/signed-token HTTP; no remote production service |
| Supabase | Optional public fictional borrower directory | Local SQL RLS/write-denial and browser fallback tests; live release uses bundled labels |
| PaddleOCR-VL / PP-DocLayoutV3 | Opt-in trusted native OCR/layout worker, observed EOS and durable review | Actual financial/policy/degraded/blank fixtures and review quarantine; no broad OCR accuracy, public-file sandbox or automatic approval |
| PP-OCRv6 | Diagnostic comparison on an observed degraded-footer failure | Recovered numeric strings/footer but failed exact row labels and table association; not promoted to automatic fallback |
| GitHub Actions / uv | Locked dependency/build/test/coverage/evaluation workflow and local release checks | Workflow is manual-dispatch only under no-spend; checked-in CI configuration and local suites do not establish a hosted run |

Implementation references: [Cortex runtime](../infra/cortex/README.md),
[OpenSearch](../infra/opensearch/README.md), [Redis](../infra/redis/README.md),
[AWS](../infra/aws/README.md), [evaluation](evaluation.md), [observability](observability.md),
[retrieval experiments](../infra/retrieval/README.md).

Additional concrete paths: [browser deployment](../infra/huggingface/browser/DEPLOYMENT.md),
[SQS-compatible worker](../infra/sqs/README.md), [OCR](../infra/ocr/README.md),
[RPC](../infra/rpc/README.md), [Supabase](../infra/supabase/README.md),
[Spark](../infra/spark/README.md), [FAISS](faiss-benchmark.md),
[Weaviate](../infra/weaviate/README.md) and [release checks](release-verification.md).

The original plan also named Vercel, S3, Snowpark, RDS, ElastiCache, managed OpenSearch,
EC2 and Lightsail. Vercel is not the delivered host; HF Static is. Source bytes use the
implemented tenant-separated `LocalSourceStore`, not S3. Financial calculations use Python
Decimal, not Snowpark. RDS/ElastiCache/managed OpenSearch are operator architecture options,
not deployed equivalents of the tested local PostgreSQL/Redis/OpenSearch services. EC2 and
Lightsail remain deployment alternatives, while the unapplied Terraform reference uses Fargate.

Query intent classification and a bounded evidence-grounding rewrite are implemented. Selective
HyDE, conversational follow-up rewriting and MMR from the original plan have not been promoted
as implemented retrieval features. The embedding LRU and signed retrieval cache have concrete
roles; they do not establish a production re-embedding migration/rollback service.

The strongest retained resume measurements are 3,840 synthetic pages, 240 authored adversarial
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
