# PySpark metadata backfill benchmark

This optional job rebuilds scoped index metadata from canonical page JSONL using
both serial Python and real PySpark DataFrames. It preserves tenant, borrower,
document/version/page, ACLs, validity dates, source hash and Unicode text length.
It validates each text hash, rejects conflicting page identities and collapses
identical replay rows. Output is staged metadata; it is not published to SQL or a
search service and does not replace the ingestion worker's authorization checks.

The benchmark reads the 3,840-page synthetic corpus once and as a tenfold replay.
The latter is 38,400 input records of the same 3,840 pages, not novel evidence.
Local Spark worker counts are one and two; this is not a distributed cluster test.

## Run without cloud services

Docker Desktop is required. Download the exact image from `compose.yaml` if it is
not present; the compose configuration never pulls automatically. Java and PySpark
are provided by that pinned image, keeping them out of the application environment.

From the repository root, choose a fresh external evidence directory:

```powershell
New-Item -ItemType Directory ../resources/credit_lens/evals/my-spark-run
$env:CREDITLENS_SPARK_PAGES = (Resolve-Path ../resources/credit_lens/corpus/pages.jsonl).Path
$env:CREDITLENS_SPARK_OUTPUT = (Resolve-Path ../resources/credit_lens/evals/my-spark-run).Path
docker compose -f infra/spark/compose.yaml run --rm benchmark
```

The container has no external network, publishes no ports, uses at most two CPUs
and 2 GiB RAM, and mounts code/input read-only. An explicit loopback hostname lets
Spark's local JVM resolve itself while networking stays disabled. The disposable
container is removed on exit. No persistent service or paid infrastructure is used.

`run/report.json` retains startup and pipeline timings, source/script hashes and
semantic output hashes. Serial timing includes read, validation, transform and
JSON write. Spark pipeline timing includes those stages plus count/uniqueness
actions and execution overhead, excluding separately measured Spark startup.
The verifier compares every output row independently of JSON whitespace or partition
order. Spark event logs, input replays and both sets of output records are retained.

Outputs are fresh-path only. A failure remains marked failed and may leave staging
files, which must not be treated as published data. The runner never overwrites an
existing run or changes the original corpus. Repeated runs use a new directory.

## Verification and limits

`controls.py` exercises both real implementations on Unicode text, nullable borrower
identity, ACL ordering, identical duplicates, corrupt hashes and conflicting
duplicate identities. To run it in the same isolated container, override the command:

```powershell
docker compose -f infra/spark/compose.yaml run --rm benchmark --conf spark.ui.enabled=false /work/controls.py
```

The serial reference runs before Spark, which can warm filesystem caches. JVM startup,
JIT and filesystem effects differ between worker-count runs. There is one trial per
configuration, so no stable speedup or scaling claim follows. Canonical-input schema
and admission checks remain the upstream ingestion system's responsibility.

Spark uses built-in [SHA-256 expressions](https://spark.apache.org/docs/4.0.1/api/python/reference/pyspark.sql/api/pyspark.sql.functions.sha2.html)
and DataFrame projection/deduplication instead of Python UDFs. This experiment answers
whether Spark overhead is justified for this backfill size; it does not add Spark
to the interactive request path.

## Observed local result

| Input records | Serial Python | Spark local[1] | Spark local[2] |
| --- | --- | --- | --- |
| 3,840 | 0.326 s | 17.572 s | 6.001 s |
| 38,400 repeated records | 1.802 s | 11.159 s | 5.186 s |

Every output matched all 3,840 projected source rows in a separate host-side
verification. The first Spark session startup took 4.318 seconds; the second context
startup reused the process/JVM and took 0.294 seconds. These startup values are
excluded from the table. Warmup and ordering effects preclude attributing the
worker-count difference entirely to parallelism. The measurements support keeping
serial Python for this corpus size; no Spark speedup over Python is claimed.

Evidence is retained in `resources/credit_lens/evals/spark-backfill-v1/run`, with
control and execution logs under `resources/credit_lens/evidence`. The run used
Spark 4.0.1 and the pinned container digest, with matching source and script hashes.
