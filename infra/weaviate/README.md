# Local Weaviate retrieval laboratory

This optional experiment compares exact NumPy with actual Weaviate HNSW searches
on the frozen synthetic corpus. It is a laboratory, not a production provider or
the public Hugging Face backend. No hosted APIs or model services are used.

Prerequisites: Docker Desktop, the Python `retrieval` extra, and the pinned local
MiniLM embedding directory described in `infra/retrieval/README.md`. Download the
exact image in `compose.yaml` if absent; automatic pulls are disabled.

```powershell
docker compose -f infra/weaviate/compose.yaml up -d
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
.venv\Scripts\python.exe -m infra.weaviate.benchmark --pages ../resources/credit_lens/corpus/pages.jsonl --models ../resources/credit_lens/local-models --output ../resources/credit_lens/evals/my-weaviate-run
```

Choose a fresh output directory. The runner builds two graphs with maxConnections
8 and 16, efConstruction 80, and searches each at ef 16 and 64. It forces
flatSearchCutoff=0 and disables vector quantization. Actual read-back schemas are
retained with per-case ANN/exact IDs, relevance scores, candidate counts and HTTP
timings. Source/input hashes and failure/completion status are in the manifest.

Canonical authorization resolves the allowed chunk IDs before each query. A
`ContainsAny` filter on a field-tokenized identity limits results, and the client
rejects unknown or out-of-scope IDs before scoring. Raw duplicate IDs are retained
with a duplicate count and scored only once; underfilled results are not padded. Only synthetic chunk IDs and
vectors are imported, not source text or credentials. This filter experiment does
not replace authenticated server grants, revocation checks or protected audits.

The local port binds to loopback, with anonymous access solely for this disposable
synthetic experiment. Do not expose it to a network or import confidential data.
The container is limited to two CPUs and 1 GiB; remote modules and telemetry are
disabled. Query vectors are warmed outside HTTP timing. Embedding and graph import
time are measured separately. Results are not production latency or scale claims.

Run the real database controls and clean up this stack:

```powershell
$env:CREDITLENS_TEST_WEAVIATE = '1'
.venv\Scripts\pytest.exe tests/test_weaviate_lab.py -q
Remove-Item Env:CREDITLENS_TEST_WEAVIATE
docker compose -f infra/weaviate/compose.yaml down
```

The runner deletes only collections it created successfully. Failed runs retain
partial evidence; an interrupted process may leave its uniquely named collection
until the disposable container is removed. There is no persistent volume.

Configuration follows the official [vector index reference](https://docs.weaviate.io/weaviate/config-refs/indexing/vector-index)
and [conditional filters reference](https://docs.weaviate.io/weaviate/api/graphql/filters).

The first full run exposed duplicate server results and stopped. The revised runner
retains those diagnostics explicitly; this observed behavior prevents treating the
experiment as a production-readiness result.

## Observed comparison

All four configurations completed 240 cases: 235 searches and five expected denials. Gold metrics use 220 eligible cases. The independent verifier recomputed overlap, page scores, scope membership and duplicate counts from retained IDs.

| M | ef | Recall@10 | nDCG@10 | ANN set agreement | HTTP search p95 | Import time | Duplicate IDs |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 8 | 16 | 55.46% | 0.4810 | 54.04% | 14.76 ms | 3.83 s | 1 |
| 8 | 64 | 56.31% | 0.4872 | 54.64% | 14.11 ms | 3.83 s | 1 |
| 16 | 16 | 59.02% | 0.4712 | 39.36% | 13.00 ms | 2.87 s | 4 |
| 16 | 64 | 59.05% | 0.4728 | 40.09% | 12.68 ms | 2.87 s | 4 |

This run does not justify promoting Weaviate over the existing retrieval workflow. More connections did not improve ANN set agreement. Repeated synthetic content, graph construction and filtering may affect the result; the run does not isolate their causal contributions. There was one graph construction per M value and no repeated-run uncertainty estimate. Import timing covers synchronous batch ingestion after collection creation, excluding embedding.

Raw v1 failure and v2 completed artifacts remain in the private resources directory. The completed run used reviewed uncommitted changes on 2b5b9fb with stable source/input hashes. The server was Weaviate 1.39.2, quantization disabled, filterStrategy acorn and efConstruction 80.

Recompute the retained results without running models or contacting Weaviate:

```powershell
.venv\Scripts\python.exe -m infra.weaviate.verify --pages ../resources/credit_lens/corpus/pages.jsonl --output ../resources/credit_lens/evals/weaviate-hnsw-v2
```
