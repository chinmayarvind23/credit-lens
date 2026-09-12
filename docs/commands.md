# Commands

Run from the repository root in a Git checkout:

```powershell
uv sync --locked
uv run --no-sync uvicorn creditlens.api:create_app --factory --host 127.0.0.1 --port 8000
```

Build the workbench as described in the [README](../README.md#setup). `/health` checks the process; `/ready` checks the initialized workflow. `/docs` exposes the API contract. The default local identity uses synthetic data.

## Development checks

```powershell
uv sync --locked --extra queue --extra retrieval --extra observability --extra admin
uv run --no-sync ruff check src
uv run --no-sync ruff format --check src
uv run --no-sync mypy src
uv run --no-sync pytest tests infra/huggingface/tests .github/tests --ignore=tests/test_retrieval_lab.py
```

Optional integration checks require the services and model files described in the corresponding `infra` guide. Run the workbench's typecheck, test and build scripts from [apps/web](../apps/web/README.md).

[Offline evaluation](../evals/README.md) and [semantic evaluation](../infra/evaluation/README.md) accept explicit output directories. Keep generated run artifacts outside the source checkout. Hosted CI uses manual dispatch; follow the repository workflows when selecting checks.
