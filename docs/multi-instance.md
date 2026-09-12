# Concurrent HTTP and separate server processes

`scripts/benchmark_multi_instance.py` starts two independent local Python server
processes with a shared PostgreSQL catalog, default request limits, warm response
caches and OTel/Prometheus telemetry. It sends 100 measured requests at each of
three concurrency levels, then verifies page revocation, current-grant denial,
quota errors and fresh SQL audit IDs on both replicas. No serving code or quota
is replaced by a benchmark stub.

| Client concurrency | Measured requests | HTTP p95 | Requests/second |
| --- | --- | --- | --- |
| 1 | 100 | 86.40 ms | 15.73 |
| 4 | 100 | 99.20 ms | 47.34 |
| 8 | 100 | 300.17 ms | 35.01 |

Each phase uses fresh processes and a unique catalog. All measured packets match
the corresponding serial warmup packet. All three phases pass the existing
2,700 ms ceiling. The higher-concurrency phase has lower throughput than the
four-client phase; this run does not establish monotonic scaling or a maximum
sustainable capacity.

Across warmup, load and follow-up requests, 360 successful responses have distinct
IDs matching 360 actual PostgreSQL audit rows. Six requests after grant revocation
return 403; 24 overload requests return `rate_limited` with 429. Both replicas
exclude the revoked physical page and invalidate their response caches. All six
child processes exited normally. Source hashes remained stable. The original run
manifest records the uncommitted benchmark script explicitly; its hash is retained.

Reproduce with Python dependencies from the README check command and a fresh,
owned PostgreSQL fixture from [the PostgreSQL guide](../infra/postgres/README.md).
Use a different unused local port if needed. The exact database must be empty:

```powershell
$env:CREDITLENS_TEST_POSTGRES_URL='postgresql+psycopg://postgres:creditlens-test-only@127.0.0.1:15432/creditlens_test'
uv run --no-sync python -m scripts.benchmark_multi_instance --owned-empty-fixture --output ../creditlens-multi-instance-results
```

The runner accepts only PostgreSQL psycopg on 127.0.0.1 and database
`creditlens_test`, rejects existing grants/audits and never drops tables. It writes
synthetic grants and pages, so use only the disposable fixture you created.
Stop that fixture when finished. The runner closes only its own server processes.
It retains complete response records, per-replica traces/metrics and a hashed
report outside the repository. Failed runs remain failed and retain collected rows.

This is a five-borrower, 330-page, lexical local workload with client-directed
request distribution. It does not exercise a load balancer, neural inference,
autoscaling, fault recovery or production identity. The database fixture had one
CPU and 512 MB memory; API processes ran on a shared workstation. Caches and quotas
remain per process: adding replicas increases the combined allowance. Shared
quota enforcement remains necessary before treating that limit as deployment-wide.
No AWS resources, paid inference or cloud cost estimate are involved.
