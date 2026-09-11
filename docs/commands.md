# CreditLens Commands

## Implemented commands

Run from the repository root:

```sh
uv sync --locked
uv run uvicorn creditlens.api:create_app --factory --host 127.0.0.1 --port 8000
uv run pytest tests/test_auth.py tests/test_api.py
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

The local mode is a fixed synthetic identity. `/health` reports process health.
`/ready` verifies the initialized local workflow. Production remains unavailable
until a governed workflow is configured and verified.
`/api/v1/borrowers` returns the current grant's synthetic borrower choices.
`POST /api/v1/query` accepts `borrower_id`, `question`, and optional `effective_at`.
It returns an evidence-only packet using the local lexical control, with cited
Decimal DSCR calculations where requested. The disposition covers DSCR only.
`GET /api/v1/evidence/{chunk_id}` rechecks current borrower/date authorization.
`/docs` and `/openapi.json` describe implemented API routes.

Configuration uses `CREDITLENS_` environment variables documented in `.env.example`.
Pass a private environment file through uvicorn's `--env-file` option if needed.
The repository's existing `.env` is not automatically loaded.

## Planned command interface

The following Make targets are design goals and do not exist yet:

```bash
make dev
make test
make test-security
make lint
make typecheck
make eval-smoke
make eval-full
make benchmark-retrieval
make benchmark-hnsw
make benchmark-context
make load-test
make quality-gate
make docker-up
make docker-down
make terraform-fmt
make terraform-validate
make terraform-plan
```

Python:

```bash
uv sync
uv run pytest
```

Frontend:

```bash
bun install
bun test
bun run dev
```

Keep local and CI commands synchronized.
