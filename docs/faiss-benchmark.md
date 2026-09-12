# Scoped FAISS benchmark

The optional laboratory compares FAISS HNSW with exact NumPy inner-product search
over normalized MiniLM vectors. Both paths receive the same currently authorized
candidate chunks. HNSW uses M=16, efConstruction=80 and efSearch=64 with one FAISS
thread. The scope index is rebuilt per query rather than shared across principals.

Install the `retrieval` extra alongside your other extras and download the pinned
embedding model using the retrieval setup guide. Then run from the repository root:

```powershell
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
.venv\Scripts\python.exe -m scripts.benchmark_faiss --pages ../resources/credit_lens/corpus/pages.jsonl --models ../resources/credit_lens/local-models --output ../resources/credit_lens/evals/my-faiss-run
```

The output directory must be new. `manifest.json` records input/source hashes,
completion status and HNSW parameters. `records.jsonl` retains all 240 cases,
including expected access denials, ordered approximate/exact chunk IDs, inner
products, candidate counts, index construction time and search time.

Recompute set agreement as the intersection size divided by the exact result set
size. A denied case has empty sets and a null score. Agreement is not gold-document
recall: two methods can agree while returning irrelevant evidence. Ties between
identical vectors can also reduce ID agreement without changing similarity.

Search timing uses cached query vectors and excludes embedding, permission
resolution, HTTP, packet construction and audits. Index construction is reported
separately. These measurements do not establish production latency or capacity.

The older `dense-hybrid-reviewed-1` experiment remains historical evidence. Its
scalar ANN overlaps lack retained IDs and cannot be independently reconstructed
without rerunning. The new runner addresses that evidence gap without altering
older scores or substituting ANN agreement for the application's Recall@10.

The [FAISS index reference](https://github.com/facebookresearch/faiss/wiki/Faiss-indexes) describes the HNSW parameters and normalized inner-product search.

## Observed local run

The auditable 240-case run returned identical top-10 chunk sets for all 235
permitted cases (100% set agreement) and preserved five access denials. A separate
verification recomputed every overlap from retained IDs and checked both result
sets against freshly resolved authorized candidates. The observed p95 was 7.81 ms
for scope-index construction and 0.224 ms for cached-vector search. Corpus embedding
took 69.79 seconds. These are small scoped synthetic indexes on a shared workstation,
not a full-corpus global search or end-to-end latency measurement.

Raw records, the manifest and independent verification are retained privately in
`resources/credit_lens/evals/faiss-auditable-v1`. The run used uncommitted reviewed
changes on eb3269c, with stable source and input hashes throughout execution.
