# Local Weaviate test service

For authenticated persistent vector search, follow [the production setup](../../docs/weaviate.md), including the separate [self-hosted HTTPS service](../../docs/weaviate.md#optional-self-hosted-vector-service). The `compose.yaml` service described below runs disposable synthetic integration checks on loopback. It enables anonymous access and has no persistent volume. Keep confidential data out of this container.

Docker Desktop and the Python `retrieval` extra are required. Download the exact pinned image in `compose.yaml` if absent; automatic pulls are disabled.

```powershell
docker compose -f infra/weaviate/compose.yaml up -d
$env:CREDITLENS_TEST_WEAVIATE = '1'
uv run --no-sync pytest tests/test_weaviate_lab.py -q
Remove-Item Env:CREDITLENS_TEST_WEAVIATE
docker compose -f infra/weaviate/compose.yaml down
```

The container binds only to loopback and disables remote modules and telemetry. Tests create isolated collections and remove their own data. Remove the disposable container after use, including interrupted runs.

Vector configuration and filtering follow the official [vector index reference](https://docs.weaviate.io/weaviate/config-refs/indexing/vector-index) and [conditional filters reference](https://docs.weaviate.io/weaviate/api/graphql/filters).
