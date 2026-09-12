# CreditLens delivery reference

- [Interactive Hugging Face demo](https://huggingface.co/spaces/chinmayarvind/creditlens)
- [Original stack and implementation evidence](stack-evidence.md)
- [Reproducible measurements and limitations](evaluation.md)
- [Architecture](HLD.md) and [implementation contracts](LLD.md)
- [Fresh installation verification](release-verification.md)
- [Concurrent HTTP and shared-authority verification](multi-instance.md)
- [Governed Cortex configuration](../infra/cortex/README.md)
- [Optional AWS operator setup](../infra/aws/README.md)

The public Space uses the static SDK to host an interactive Python browser worker. Its observed
release is `e964da2860f2ff4f7f5987e1986adb9457563fbd`, with runtime `RUNNING` and no requested
compute hardware. The app page was reachable during the current delivery check. This release was
checked locally and on the public Space for borrower loading, a new DSCR packet (1.5000) and exact
financial-source inspection. The earlier 12-check desktop/mobile and offline run applies to the
preceding release. The existing recording was preserved with its byte hash verified after
publication. The optional Supabase directory remains unconfigured in this self-contained demo.

An earlier governed-runtime regression passed 498 tests with one optional neural integration
skipped. This is separate from the earlier fresh-checkout run and from the retained local
neural-retrieval experiments. No new cloud or inference performance measurements are implied.

For resume use, retain 3,840 synthetic pages, the 240-question authored suite and local Recall@10
improvement from 74.05% to 86.54%. Do not present requested semantic accuracy or cloud-cost targets
as measured results. The full-packet DeepEval v2 diagnostic now has offline reconciliation of all
235 packets and five denials: 102 raw passes (43.4%), with 8/8 controls. `human_calibrated` and
`validated_project_quality` remain false. A recorded adapter retained 192 judgments and added 43
unstarted cases with unchanged empty evidence lists. Agent review found disputed judge failures;
this rate is not reliable accuracy and is omitted from resume headline metrics. RAGAS cited-field
support is a separate narrower diagnostic. See [evaluation](evaluation.md).

The project resources directory contains the resume bullets, blog, interview notes, decisions, raw
measurements and video. Subsequent verified work includes the trusted Windows OCR worker and durable
review queue, [Grafana operations dashboards](../infra/monitoring/README.md), [scoped FAISS
comparison](faiss-benchmark.md), [four Weaviate HNSW configurations](../infra/weaviate/README.md),
and [local Spark metadata backfill](../infra/spark/README.md). Live managed deployments, sandboxed
public OCR ingestion and the full expanded PRD are not represented as complete.

For a concise performance claim, the local two-process extractive benchmark recorded 300 ms p95 at
eight concurrent clients and 360 distinct PostgreSQL audit records across load, warmup and follow-up
checks. Its five-borrower lexical workload is separate from the 3,840-page hybrid retrieval
experiment. The latter measured .7878 nDCG@10 and a 12.49-percentage-point Recall@10 improvement.

The optional [Supabase public directory](../infra/supabase/README.md) has passed 16 actual local SQL
permission checks and frontend contract tests. This release passed 38 frontend tests, 15 HF
packaging checks, three publisher regression tests, TypeScript checks and the frontend production
build. Managed Supabase REST verification remains separate from these local implementation checks.

The local monitoring bundle has 23 operational, six evaluation and nine indexing panels, with six
alert rules. Runtime cost metrics distinguish known/unknown observations and exclude unknown dollar
costs from histograms. RAGAS raw calls record 538,444 prompt and 65,932 completion tokens.
Evaluation/imported snapshots are distinct from live operational metrics.

The latest OpenSearch snapshot acknowledged 3,840 chunks in 0.957 seconds and observed one canary
searchable after 0.990 seconds. A separate deliberately invalid write was rejected. This does not
establish continuous production freshness or embedding failure rates. Shared Redis quota
verification passed 24 targeted tests; the separate [PostgreSQL restore drill](recovery.md) passed
one test preserving 330 canonical rows, protected audits and snapshot revocations. Post-backup
revocations must be reconciled before restored service reopens. Full PRD completion and production
readiness remain unclaimed.

The later local publication experiment verified all 3,840 SQL canonical payloads and exact
searchable text in 4.890 seconds after publication, using 39 search requests. SQL publication took
32.057 seconds separately. The actual pinned CPU neural drill recorded one document-embedding
invocation, three query-embedding invocations including one injected failure, and one rerank
invocation. Recovery reproduced the original ranking, and all three neural Grafana panels were
verified. These checks extend the earlier canary snapshot; they do not establish continuous
production freshness or a natural model-error rate. The bundle now contains 38 panels: 23
operational, six evaluation and nine indexing. See [publication
visibility](../infra/monitoring/publication-visibility.md) and [neural operation
monitoring](../infra/monitoring/neural.md).

The final repository regression passed 551 tests with five opt-in integrations skipped.
A separate real pinned-model TCP HTTP integration then passed, covering five financial
scenarios, exact source inspection, denied scope, abstention, overload and grant
revocation with external sockets prohibited. The source-level neural instrumentation
also passed 27 targeted tests; the new drill failure-report checks passed seven.
Formatting-only source changes retain matching Python AST records privately.

Current public release verification reached the canonical Static host over IPv4 and
verified all 15 application/runtime files and the recorded GIF against their hashes.
The Space API reports RUNNING with no requested compute hardware. Initial browser
and default-network requests reset, so this check does not claim fresh browser
execution; the earlier interactive release QA remains separately recorded.
