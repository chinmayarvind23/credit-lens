# PySpark metadata backfill

This optional job rebuilds scoped index metadata from canonical page JSONL using
both serial Python and real PySpark DataFrames. It preserves tenant, borrower,
document/version/page, ACLs, validity dates, source hash and Unicode text length.
It validates each text hash, rejects conflicting page identities and collapses
identical replay rows. Output is staged metadata; it is not published to SQL or a
search service and does not replace the ingestion worker's authorization checks.

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

The verifier compares every output row independently of JSON whitespace or partition order. Retain generated records outside the source checkout.

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

Spark uses built-in [SHA-256 expressions](https://spark.apache.org/docs/4.0.1/api/python/reference/pyspark.sql/api/pyspark.sql.functions.sha2.html)
and DataFrame projection/deduplication instead of Python UDFs. The backfill command is separate from the interactive request path.
