# CreditLens delivery reference

- [Interactive Hugging Face demo](https://huggingface.co/spaces/chinmayarvind/creditlens)
- [Original stack and implementation evidence](stack-evidence.md)
- [Reproducible measurements and limitations](evaluation.md)
- [Architecture](HLD.md) and [implementation contracts](LLD.md)
- [Fresh installation verification](release-verification.md)
- [Concurrent HTTP and shared-authority verification](multi-instance.md)
- [Governed Cortex configuration](../infra/cortex/README.md)
- [Optional AWS operator setup](../infra/aws/README.md)

The public Space uses the static SDK to host an interactive Python browser worker.
Its observed release is `e964da2860f2ff4f7f5987e1986adb9457563fbd`, with runtime
`RUNNING` and no requested compute hardware. The app page was reachable during the
current delivery check. This release was checked locally and on the public Space for borrower loading,
a new DSCR packet (1.5000) and exact financial-source inspection. The earlier
12-check desktop/mobile and offline run applies to the preceding release. The
existing recording was preserved with its byte hash verified after publication.
The optional Supabase directory remains unconfigured in this self-contained demo.

The latest governed-runtime regression passed 498 tests with one optional neural
integration skipped. This is separate from the earlier fresh-checkout run and
from the retained local neural-retrieval experiments. No new cloud or inference
performance measurements are implied.

For résumé use, retain 3,840 synthetic pages, the 240-question authored suite and
local Recall@10 improvement from 74.05% to 86.54%. Do not present requested semantic
accuracy or cloud-cost targets as measured results. The later full-packet DeepEval
diagnostic was stopped when the user prioritized faster résumé delivery; its raw
outputs and failures remain retained. RAGAS's completed cited-field diagnostic
does not establish whole-answer accuracy.

The project resources directory contains the résumé bullets, blog,
interview notes, decisions, raw measurements and video. Subsequent verified work
includes the trusted Windows OCR worker and durable review queue,
[Grafana operations dashboards](../infra/monitoring/README.md),
[scoped FAISS comparison](faiss-benchmark.md),
[four Weaviate HNSW configurations](../infra/weaviate/README.md), and
[local Spark metadata backfill](../infra/spark/README.md).
Live managed deployments, sandboxed public OCR ingestion and the full expanded
PRD are not represented as complete.

For a concise performance claim, the local two-process extractive benchmark
recorded 300 ms p95 at eight concurrent clients and 360 distinct PostgreSQL audit
records across load, warmup and follow-up checks. Its five-borrower lexical
workload is separate from the 3,840-page hybrid retrieval experiment. The latter
measured .7878 nDCG@10 and a 12.49-percentage-point Recall@10 improvement.

The optional [Supabase public directory](../infra/supabase/README.md) has passed
16 actual local SQL permission checks and frontend contract tests. This release
passed 38 frontend tests, 15 HF packaging checks, three publisher regression tests,
TypeScript checks and the frontend production build. Managed Supabase REST
verification remains separate from these local implementation checks.
