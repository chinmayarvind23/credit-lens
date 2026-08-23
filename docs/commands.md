# CreditLens Commands

Target one-command interface:

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
