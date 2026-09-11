# Local Redis verification

The retrieval cache is an optional provider wrapper. The HTTP workflow does not
enable it yet. It signs opaque request keys and source IDs, reauthorizes every
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
