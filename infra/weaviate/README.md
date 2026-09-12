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
.venv\Scripts\python.exe -m infra.weaviate.benchmark --pages ../creditlens-work/corpus/pages.jsonl --models ../creditlens-work/local-models --output ../creditlens-work/evals/my-weaviate-run
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

Recompute a saved run without inference or a running Weaviate service:

```powershell
.venv/Scripts/python.exe -m infra.weaviate.verify --pages <pages.jsonl> --output <saved-run-directory>
```
