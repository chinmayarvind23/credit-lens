# syntax=docker/dockerfile:1
# Tool versions match the locally verified lockfile workflow.
FROM oven/bun:1.3.10@sha256:b86c67b531d87b4db11470d9b2bd0c519b1976eee6fcd71634e73abfa6230d2e AS frontend
WORKDIR /web
COPY apps/web/package.json apps/web/bun.lock ./
RUN bun install --frozen-lockfile
COPY apps/web/index.html apps/web/tsconfig.json ./
COPY apps/web/src ./src
RUN bun run typecheck && bun run build

FROM ghcr.io/astral-sh/uv:0.8.4@sha256:40775a79214294fb51d097c9117592f193bcfdfc634f4daa0e169ee965b10ef0 AS uv
FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d AS dependencies
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
# No development or optional provider extras enter the demo image.
RUN uv sync --locked --no-dev --no-install-project --python /usr/local/bin/python

FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d AS runtime
RUN useradd --create-home --uid 1000 appuser
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/src
COPY --from=dependencies --chown=1000:1000 /app/.venv /app/.venv
COPY --chown=1000:1000 src/creditlens ./src/creditlens
COPY --from=frontend --chown=1000:1000 /web/dist ./apps/web/dist
COPY --chown=1000:1000 infra/huggingface/demo_entrypoint.py ./demo_entrypoint.py
RUN mkdir /app/data && chown 1000:1000 /app/data
USER 1000:1000
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/health', timeout=3).read()"]
CMD ["/app/.venv/bin/python", "/app/demo_entrypoint.py"]
