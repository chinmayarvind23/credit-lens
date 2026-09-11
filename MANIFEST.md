# CreditLens repository map

- `PRD.md`: product requirements and explicitly unmeasured targets.
- `README.md`: current status, setup and documentation links.
- `pyproject.toml`, `uv.lock`, `.python-version`: Python environment.
- `src/creditlens`: backend package.
- `apps/web`: Bun and TypeScript browser application.
- `docs`: public system design and operational contracts.
- `docs/adr`: accepted architecture decisions.

Private plans, execution instructions, decisions, audit reports and measured
evidence live outside this Git repository in `resources/credit_lens` within the
parent workspace. The existing `.env` is local and ignored by Git.
