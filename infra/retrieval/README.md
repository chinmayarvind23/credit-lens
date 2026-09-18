# Local hybrid retrieval

The optional serving path combines local BM25, MiniLM dense retrieval, reciprocal
rank fusion and a cross-encoder. It runs on CPU, loads reviewed files at startup
and makes no remote inference calls. The public HF browser runtime uses lexical retrieval. Enabling this option locally does not change
that deployment. For persistent vector search in production mode, follow [Weaviate setup](../../docs/weaviate.md).

Install the locked optional dependencies, preserving the queue SDK if using it:

```powershell
uv sync --locked --extra retrieval --extra queue
```

Prepare a trusted model directory outside the repository with `embedding` and
`reranker` subdirectories. The required repositories, immutable revisions and
file SHA256 values are in [model_manifest.json](../../src/creditlens/model_manifest.json).
The existing `creditlens.retrieval_lab.model_snapshot` helper can download those
public model snapshots during operator setup. Downloads are a setup step, never
part of request handling. Keep the directory read-only during serving.

```powershell
$env:CREDITLENS_RETRIEVAL_MODE='hybrid'
$env:CREDITLENS_LOCAL_MODEL_DIRECTORY='C:\models\creditlens'
uv run --no-sync uvicorn creditlens.api:create_app --factory --host 127.0.0.1 --port 8000
```

Use your prepared directory in the second command. Missing, changed or unexpected
model files fail startup. Both models use local-only loading, safetensors and
`trust_remote_code=False`. The default Docker image has neither ML dependencies
nor model weights; it remains a small lexical demo and isolated PDF parser.

Current SQL grants and canonical tenant, borrower, ACL and effective-date scope
are selected before model scoring. Both hybrid branches are required. Reranking
retains the original branch results for revocation and citation checks. A model
failure fails the request, and final finance, context, exact-citation and audit
checks remain in the normal workflow.

Packet admission requires a non-generic question term in authorized ranked text.
This prevents nearest-neighbor results or generic words from turning an unrelated
question into a lending packet. It can abstain on a valid semantic paraphrase with
no shared words and cannot establish support for every qualifier.

The bounds are 512 authorized dense candidates, 100 results per search branch,
40 fused reranking candidates and ten final ranked chunks. Model input is capped
at 2,000 question characters, 16,000 UTF-8 bytes per chunk and one million total
chunk bytes. Embeddings truncate to 256 tokens and cross-encoder pairs to 512;
long documents can therefore lose ranking information. Context selection retains
its separate 16-chunk/16,000-byte limit and explicit financial metadata lookups.

One inference executes per instance using four Torch CPU threads. Concurrent
inference returns `model_busy` with HTTP 503. The LRU retains at most 4,096 owned
384-dimensional float32 document vectors, about 6 MiB plus keys and overhead.
Weights and transient tensors require additional memory. Cache keys bind tenant,
chunk ID and actual text hash; only currently authorized candidates can reuse
vectors. Unused revoked vectors are eventually evicted, not immediately erased.
Questions and query vectors are not cached. These bounds are not a hard elapsed
time deadline; configure process supervision for the host deployment.

If Redis is also enabled, its signed result cache is bound to the full model
manifest and algorithm configuration. Every hit rehydrates current canonical
records and resolves current grants. Redis still defaults off.

Actual-model HTTP checks require an explicit prepared directory:

```powershell
$env:CREDITLENS_TEST_MODEL_DIRECTORY=$env:CREDITLENS_LOCAL_MODEL_DIRECTORY
uv run --no-sync pytest tests/test_neural_integration.py -q
```

This test uses an actual loopback TCP server and blocks external socket
connections. It checks financial scenarios, source retrieval, denied scope,
abstention, model overload and grant revocation. Numeric unit tests use explicit
model doubles and do not establish model quality.

## CPU container variant

The optional Docker target `neural-runtime` installs hashed CPU wheels from
`requirements-cpu.lock`. Regenerate it with uv 0.8.4 using
`python scripts/lock_neural_runtime.py --uv /path/to/uv`. The generator constrains
resolution to `uv.lock`, preserving Python-version markers, and records source
hashes with LF-normalized newlines. PyTorch uses its official CPU package index;
the lock includes no NVIDIA or Triton packages. Linux CPU execution requires its
own verification even when the Windows package versions match.

Build with `docker build --target neural-runtime -t creditlens-neural:release .`.
Mount the verified model directory read-only at `/models`. The public entrypoint
accepts only `CREDITLENS_DEMO_RETRIEVAL=lexical` or `hybrid`; all other ambient
CreditLens settings are ignored. Synthetic memory evidence and local SQLite stay
fixed. Default Docker builds remain lexical. Follow the bounded-container and
publication procedure in [live deployment](../huggingface/live/DEPLOYMENT.md).
