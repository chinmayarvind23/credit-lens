# Build and stage the synthetic demo

The Dockerfile builds the frontend with Bun and installs only locked Python runtime dependencies. The final image runs as UID/GID 1000 and serves FastAPI plus the web assets on port 7860. The entrypoint fixes synthetic mode, a local SQLite path, and same-origin browser access.

## Local container

From the repository root:

```sh
docker build --tag creditlens-demo:local .
docker run --detach --name creditlens-demo-local --publish 127.0.0.1:17860:7860 --cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs /app/data:rw,noexec,nosuid,size=64m,uid=1000,gid=1000 --tmpfs /tmp:rw,noexec,nosuid,size=32m --cpus 1 --memory 512m --pids-limit 128 creditlens-demo:local
uv run python infra/huggingface/smoke_container.py --base-url http://127.0.0.1:17860
docker stop creditlens-demo-local
docker rm creditlens-demo-local
```

The smoke script checks readiness, health, web delivery, the five authorized demo borrowers, financial calculation, cited source retrieval, `no-store` API behavior, and denial of an unauthorized borrower. It exercises HTTP contracts, not browser rendering or production provider readiness.

## Reviewable upload package

Choose a fresh path outside the repository. Existing directories are never overwritten:

```sh
uv run python scripts/deploy_space.py stage --output /absolute/path/creditlens-space-stage
uv run python scripts/deploy_space.py verify --stage /absolute/path/creditlens-space-stage
docker build --tag creditlens-demo:staged /absolute/path/creditlens-space-stage
```

`stage` copies exactly the files listed in `scripts/deploy_space.py`. The Space card becomes the staged root README. Backend code for evaluation and optional retrieval experiments is excluded. No `.env`, credentials, PDFs, databases, source corpus artifacts, evaluation outputs, Git history, private resources, or installed dependencies enter the package. The manifest records SHA-256 hashes, source revision, and whether the allowlisted working tree had uncommitted changes.

Review the staged package and its manifest. `verify` rejects extra or missing files, changed bytes, symlinks, and recognizable credential formats. This is a safeguard against accidental disclosure; it does not replace reviewing source changes or prove the absence of every possible secret format.

## Explicit upload

Upload requires an existing Docker Space, its explicit `namespace/space-name`, and a current Hugging Face CLI login. The script uses an installed `hf`, or `uv tool run --from huggingface-hub==1.31.0 hf`. It never reads or prints token files, creates repositories, changes Space permissions, configures hardware, or supplies credentials on the command line.

After the package and destination have been reviewed:

```sh
uv run python scripts/deploy_space.py upload --stage /absolute/path/creditlens-space-stage --repo-id namespace/space-name
```

The script checks current login, confirms that the named Space exists, verifies the package again, and uploads that directory. It does not delete remote files. Use a dedicated Space; unrelated old files in an existing Space remain untouched. An upload does not establish a healthy deployment. Inspect the Space build, then check its actual `/health`, `/ready`, borrower/query/source workflow, and UI.

No Hugging Face account, destination, external upload, or cloud deployment is configured by these files. The synthetic runtime has ephemeral storage and does not represent the required production AWS architecture.

## CI

The quality workflow uses pinned action commits and locked Bun/Python installs. It runs static checks, Python/frontend tests, a generated synthetic corpus and local evaluation gates, staging verification, an actual Docker build, and container HTTP smoke. Evidence is retained as a workflow artifact. Cloud uploads are excluded.

CI regenerates the reviewed lexical control from commit `5d6d6ab840e8a498bf202582d1d3c5d5da62ddaa` in a separate worktree with its locked dependencies. Both control and current code receive the same physical pages and `--outcomes`; the current run must compare against the control's `summary.json`. Gold and metric contract mismatches fail the comparison. Both runs and their provenance are retained. Changing this reference requires review; the local authored-qrel comparison does not establish independent semantic quality.

## References

- [Docker multi-stage builds](https://docs.docker.com/build/building/multi-stage/)
- [uv in Docker](https://docs.astral.sh/uv/guides/integration/docker/)
- [Hugging Face Docker Spaces](https://huggingface.co/docs/hub/en/spaces-sdks-docker)
- [Hugging Face CLI](https://huggingface.co/docs/huggingface_hub/en/guides/cli)

Coverage policy and CI gates are configured in `.github/coverage_policy.json` and `.github/workflows/quality.yml`.
