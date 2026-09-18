# Local Redis verification

The retrieval cache is an optional provider wrapper enabled explicitly in the
demo or governed production HTTP workflow. It signs opaque request keys and source IDs, reauthorizes every
read and hydrates from the current canonical catalog. Redis is never a grant store.

The fixture uses the pinned Redis image digest
`sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf`.
Use an isolated local synthetic instance; the test changes only randomly keyed
entries with short TTL and has no flush command.

```powershell
docker run --detach --name creditlens-redis-check --publish 127.0.0.1:16389:6379 --user 1000:1000 --read-only --tmpfs /data:rw,noexec,nosuid,size=64m,uid=1000,gid=1000 --cap-drop ALL --security-opt no-new-privileges --memory 128m --cpus 1 --pids-limit 64 redis:7.4-alpine@sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf redis-server --appendonly no --maxmemory 64mb --maxmemory-policy allkeys-lru
$env:CREDITLENS_TEST_REDIS_URL='redis://127.0.0.1:16389/15'
uv run pytest infra/redis/test_integration.py
docker rm --force creditlens-redis-check
```

To enable the cache for the local demo, keep the isolated Redis container running
and configure both process variables. Generate a fresh signing key locally:

```powershell
$env:CREDITLENS_REDIS_URL='redis://127.0.0.1:16389/15'
$env:CREDITLENS_CACHE_SIGNING_KEY=(uv run python -c "import secrets; print(secrets.token_hex(32))")
uv run uvicorn creditlens.api:create_app --factory --host 127.0.0.1 --port 8000
```

`CREDITLENS_CACHE_TTL_SECONDS` defaults to 60 and accepts 1 through 3600 seconds.
Keep the signing key out of source control. Remote Redis requires TLS. A cache
outage records `cache.retrieval.unavailable` and recomputes the same authorized
search. Hits report `cache_hit: true` and `cache.retrieval.hit`; every hit still
recomputes finance, validates citations and writes a fresh audit record. Packet
generation remains determined by the configured workflow: extractive demo mode or
pinned Ollama synthesis. A retrieval hit does not bypass generation, its support
checks or final authority. A separate response-cache hit can reuse a validated
packet while still requiring current evidence and a new audit.
Configure the canonical catalog and search workflow before enabling a cache wrapper.
The same URL and signing-key settings enable caching around governed Cortex or
Weaviate search. Production cache identity binds the catalog, provider and retrieval
configuration; Weaviate includes its collection and pinned model revision.
Use a stable shared signing key from the deployment secret store and a restricted
Redis service for production. The disposable loopback container above is a local
verification setup, not a managed deployment.

## Shared request quotas

`CREDITLENS_QUOTA_REDIS_URL` opts HTTP and the local RPC launcher into a shared
fixed-window allowance. It is independent of the retrieval cache URL/signing key.
The free browser runtime and default demo need no Redis. Defaults are 60 admitted
requests per authenticated subject per 60 seconds. Configure `QUERY_LIMIT` (1-10000),
`QUERY_WINDOW_SECONDS` (1-3600), and `QUOTA_NAMESPACE` (1-64 alphanumeric, underscore
or hyphen characters), all with the `CREDITLENS_` prefix. Every worker in the same
deployment must use the same values and database. Query aliases and admin mutations
already using the limiter consume the same subject allowance. Denied/failed work
after admission still consumes a slot; denied admissions do not extend the window.

Use a separate quota instance or configure the shared instance with bounded
`maxmemory` and **noeviction**. The cache fixture above uses `allkeys-lru` and is not
appropriate for enforcing quotas: evicting a counter resets its allowance. Keys
contain full SHA-256 subject digests and a configured namespace, never raw subject,
query or evidence text. Authenticated grants bound access, each identity has one
fixed-size counter with a finite server TTL, and memory exhaustion fails closed.
The Lua script atomically checks, increments and attaches the initial expiry.
Missing TTL, invalid state, transport timeouts and capacity errors return curated
503; exhaustion returns 429. There is no local fallback or ambiguous-write retry.
Remote connections require TLS; operators must restrict Redis ACL/network access.

Application restarts retain server counters. Redis reconnection and TTL expiry
recover automatically. Redis restart without persisted counters resets allowance;
configure persistence according to your quota-reset policy. This is an abuse
quota, not a durable billing ledger. Lua EVAL includes its source on each request,
so server script-cache loss does not require application recovery.

For local evidence, create an owned disposable container using the command above
with a fresh name, port 16390 and `--maxmemory-policy noeviction`, then run:

```powershell
$env:CREDITLENS_TEST_QUOTA_REDIS_URL='redis://127.0.0.1:16390/15'
.venv/Scripts/python.exe -m pytest tests/test_shared_quotas.py -q
```

Only point this test variable at that owned fixture: the recovery test briefly
pauses the entire Redis server with `CLIENT PAUSE`. It does not flush data or
change server configuration. Remove only the fixture container afterward.
