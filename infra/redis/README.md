# Local Redis verification

The retrieval cache is an optional provider wrapper enabled explicitly in the
demo HTTP workflow. It signs opaque request keys and source IDs, reauthorizes every
read and hydrates from the current canonical catalog. Redis is never a grant store.

The verified server is Redis7.4.11, image digest
`sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf`.
Use an isolated local synthetic instance; the test changes only randomly keyed
entries with short TTL and has no flush command.

```powershell
docker run --detach --name creditlens-redis-check --publish 127.0.0.1:16389:6379 --user 1000:1000 --read-only --tmpfs /data:rw,noexec,nosuid,size=64m,uid=1000,gid=1000 --cap-drop ALL --security-opt no-new-privileges --memory 128m --cpus 1 --pids-limit 64 redis:7.4-alpine@sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf redis-server --appendonly no --maxmemory 64mb --maxmemory-policy allkeys-lru
$env:CREDITLENS_TEST_REDIS_URL='redis://127.0.0.1:16389/15'
uv run pytest infra/redis/test_integration.py
docker rm --force creditlens-redis-check
```

The test uses the actual local BM25 provider and Redis transport. Its call counter
only observes provider invocations. It verifies a hit, source citation, server
expiry, tamper repair and catalog revocation. Unit tests cover SQL revocation,
in-flight changes, malformed signatures, unsafe transport settings and outages.

The private-repository hosted workflow is manual under the user's no-spend rule.
Do not dispatch it unless account settings guarantee no charge. Equivalent local
checks remain runnable without hosted minutes. No paid Redis or cloud resource
is required for these tests. No response-cache latency or cost improvement is claimed.

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
generation remains `local-extractive`, preserving the DSCR-only UI description.
Production cache integration remains unavailable until the production catalog
and search workflow are initialized.
