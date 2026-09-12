# Neural operation monitoring

The optional local hybrid runtime passes its application-owned telemetry into the
pinned CPU ranker. Three finite stages identify actual neural work:

| Stage | Observed work |
|---|---|
| `neural.embed_documents` | One document encoder invocation plus float conversion and output validation |
| `neural.embed_query` | One uncached query encoder invocation plus output validation |
| `neural.rerank` | One cross-encoder invocation, output validation and stable ordering |

The existing duration histogram counts successful and failed invocations. The
stage error counter includes model exceptions and malformed/nonfinite output.
Cached document vectors skip document encoding. Input-budget and busy-admission
rejections happen before inference and do not count as embedding failures.
One invocation can encode several documents; these are not token or document counts.

No questions, document IDs, vectors, model paths or exception messages enter metric
labels or OTel attributes. Errors retain curated API behavior, and failed inference
releases the model lock so later requests can recover. With telemetry disabled,
the ranker uses empty contexts and retains its prior behavior.

## Verify with existing local weights

From the repository root, in an environment with the optional neural dependencies
already installed:

```powershell
python -m scripts.check_neural_telemetry --models /existing/pinned-model-directory --output /private/fresh-neural-drill
```

The command verifies local model files, uses CPU inference and never downloads
weights or calls a hosted model. It runs actual ranking and reranking, injects one
clearly labeled query-encoder exception, restores the encoder and requires the
recovered ranking to match. It retains source hashes, aggregate metrics and private
ranked IDs. This fault drill does not measure a natural model failure rate.

The final local run observed one document encoder call, three query encoder calls
(two successful, one injected failure), and one reranker call. Source hashes and
trace privacy checks passed. Twenty-seven neural/telemetry tests separately cover
numeric failures, cache behavior, lock recovery, API wiring and telemetry privacy.

Three operations dashboard panels show neural invocations, failures and mean
duration grouped by job and stage. Actual Grafana/Prometheus queries reproduced
all observed counts from the retained run. A bounded local snapshot can be served
on port19105 using `serve_snapshot.py`; its scrape job is explicitly labeled as a
local fault drill. The application also emits these stages directly during live
hybrid requests. The free lexical browser does not run these models.
