"""Measure real loopback HTTP with current grants, finance, citations and disk-backed audit."""

import argparse
import json
import os
import platform
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from secrets import token_hex
from socket import AF_INET, SOCK_STREAM, socket
from threading import Thread
from time import perf_counter, sleep
from typing import Any

import httpx
import uvicorn
from pydantic import SecretStr
from sqlalchemy import func, select

import creditlens
from creditlens.api import create_app
from creditlens.auth import authorized_page
from creditlens.cache import RedisBytes
from creditlens.corpus import build_demo_pages
from creditlens.domain import Packet
from creditlens.evaluation import git_metadata, percentile, source_hashes
from creditlens.lab_evidence import save_checkpoint
from creditlens.retrieval import chunk_page
from creditlens.settings import Settings
from creditlens.storage import audit_events

REQUESTS = tuple(
    {
        "borrower_id": f"borrower-{number:03d}",
        "question": "Prepare the DSCR underwriting packet",
        "effective_at": "2026-06-01",
    }
    for number in range(1, 6)
)


def fingerprint(packet: Packet) -> str:
    """Compare every substantive field while allowing new IDs, timing and cache provenance."""
    value = packet.model_dump_json(
        exclude={"request_id", "latency_ms", "stages", "cache_hit", "provider_mode"}
    )
    return sha256(value.encode()).hexdigest()


@contextmanager
def serve(settings: Settings, *, startup_timeout: float = 15) -> Iterator[tuple[str, Any]]:
    """Bind a dedicated ephemeral loopback port and close only this owned Uvicorn server."""
    if not 0 < startup_timeout <= 120:
        raise ValueError("Startup timeout must be between zero and 120 seconds")
    app = create_app(settings)
    with socket(AF_INET, SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        worker = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            deadline = perf_counter() + startup_timeout
            while not server.started:
                if not worker.is_alive() or perf_counter() > deadline:
                    raise RuntimeError("Benchmark HTTP server failed startup")
                sleep(0.01)
            yield f"http://127.0.0.1:{port}", app
        finally:
            server.should_exit = True
            worker.join(timeout=15)
            if worker.is_alive():
                raise RuntimeError("Benchmark HTTP server did not close")


def request_one(
    client: httpx.Client, body: dict[str, str], baseline: dict[str, str], cached: bool, warmup: bool
) -> dict[str, Any]:
    """Time complete response delivery; fail immediately on errors or changed financial content."""
    started = perf_counter()
    response = client.post("/api/v1/query", json=body)
    elapsed = (perf_counter() - started) * 1000
    response.raise_for_status()
    packet = Packet.model_validate(response.json())
    digest = fingerprint(packet)
    borrower = body["borrower_id"]
    if borrower in baseline and baseline[borrower] != digest:
        raise RuntimeError("Cached and uncached substantive packets differ")
    baseline[borrower] = digest
    if packet.cache_hit != (cached and not warmup):
        raise RuntimeError("Observed cache path differs from scheduled benchmark path")
    return {
        "borrower_id": borrower,
        "warmup": warmup,
        "http_round_trip_ms": elapsed,
        "substantive_sha256": digest,
        "packet": packet.model_dump(mode="json"),
    }


def run_block(
    output: Path, redis_url: str, cached: bool, block: int, cycles: int, baseline: dict[str, str]
) -> list[dict[str, Any]]:
    """Keep the normal 60-per-minute quota: five warmups plus at most fifty timed requests."""
    mode = "redis" if cached else "uncached"
    name = f"{block:02d}-{mode}"
    settings = Settings(
        database_url=f"sqlite:///{(output / (name + '.db')).as_posix()}",
        redis_url=SecretStr(redis_url if cached else ""),
        cache_signing_key=SecretStr(token_hex(32) if cached else ""),
        cache_ttl_seconds=3600,
    )
    rows = []
    with (
        serve(settings) as (base_url, app),
        httpx.Client(base_url=base_url, timeout=10, trust_env=False) as client,
        (output / f"{name}.jsonl").open("w", encoding="utf-8") as stream,
    ):
        principal = app.state.store.resolve("synthetic-demo")
        for cycle in range(cycles + 1):
            for body in REQUESTS:
                row = request_one(client, body, baseline, cached, cycle == 0)
                packet = Packet.model_validate(row["packet"])
                if any(
                    not authorized_page(chunk, principal, body["borrower_id"], date(2026, 6, 1))
                    for chunk in packet.evidence
                ):
                    raise RuntimeError("HTTP response exposed unauthorized evidence")
                row.update(mode=mode, block=block)
                stream.write(json.dumps(row) + "\n")
                stream.flush()
                rows.append(row)
        with app.state.store.engine.connect() as connection:
            audit_count = connection.scalar(select(func.count()).select_from(audit_events))
        if audit_count != len(rows):
            raise RuntimeError("HTTP responses do not have one durable audit each")
    return rows


def provenance(repo: Path) -> dict[str, Any]:
    """Bind source, runner, lockfile and exact workload; preserve environment without secrets."""
    return {
        "source_hashes": source_hashes(repo),
        "script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "lock_sha256": sha256((repo / "uv.lock").read_bytes()).hexdigest(),
        "workload_sha256": sha256(json.dumps(REQUESTS, sort_keys=True).encode()).hexdigest(),
    }


def finish_provenance(manifest: dict[str, Any], repo: Path) -> bool:
    """Record changed or missing code even when requests fail before the measurement completes."""
    try:
        stable = provenance(repo) == manifest["provenance"]
    except OSError as error:
        manifest.update(provenance_stable=False, provenance_error_type=type(error).__name__)
        return False
    manifest["provenance_stable"] = stable
    return stable


def execute(args: argparse.Namespace) -> None:
    """Persist partial failures and reject runs whose code changed during measurement."""
    if (
        args.repo.resolve() != Path(__file__).resolve().parents[1]
        or args.repo.resolve() != Path(creditlens.__file__).resolve().parents[2]
    ):
        raise ValueError("Benchmark repo must match the actually imported source and runner")
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "started_at_utc": datetime.now(UTC).isoformat(),
        "git": git_metadata(args.repo),
        "provenance": provenance(args.repo),
        "execution_status": "running",
        "mode": "synthetic-local-http-retrieval-cache",
        "requests": REQUESTS,
        "physical_pages": len(build_demo_pages()),
        "canonical_chunks": sum(len(chunk_page(page)) for page in build_demo_pages()),
        "blocks": args.blocks,
        "cycles": args.cycles,
        "concurrency": 1,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "dependencies": {key: version(key) for key in ("uvicorn", "httpx", "redis", "sqlalchemy")},
        "latency_scope": "HTTP request through complete body receipt, current grants, finance, "
        "citation checks and SQLite disk audit; excludes startup and five warmups per block",
        "percentile": "nearest-rank",
        "cost_usd": None,
        "limitations": "Five exposed synthetic borrowers, serial loopback requests, small corpus, "
        "shared workstation load; no production, response-cache or cloud-cost claim.",
    }
    save_checkpoint(args.output, manifest)
    rows: list[dict[str, Any]] = []
    baseline: dict[str, str] = {}
    try:
        redis_url = os.environ["CREDITLENS_TEST_REDIS_URL"]
        probe = RedisBytes(redis_url)
        try:
            manifest["redis_server_version"] = probe.client.info("server")["redis_version"]
        finally:
            probe.close()
        for block in range(args.blocks):
            for cached in (False, True) if block % 2 == 0 else (True, False):
                rows.extend(run_block(args.output, redis_url, cached, block, args.cycles, baseline))
                save_checkpoint(args.output, manifest, progress={"completed_requests": len(rows)})
        summary = {}
        for mode in ("uncached", "redis"):
            selected = [row for row in rows if row["mode"] == mode and not row["warmup"]]
            latencies = [row["http_round_trip_ms"] for row in selected]
            summary[mode] = {
                "requests": len(selected),
                "p50_http_ms": percentile(latencies, 0.5),
                "p95_http_ms": percentile(latencies, 0.95),
                "mean_http_ms": sum(latencies) / len(latencies),
                "cache_hits": sum(row["packet"]["cache_hit"] for row in selected),
            }
        manifest["execution_status"] = "completed"
        save_checkpoint(args.output, manifest, summary=summary)
        print(json.dumps(summary), flush=True)
    except Exception as error:
        manifest.update(execution_status="failed", failure_type=type(error).__name__)
        save_checkpoint(args.output, manifest)
        raise
    finally:
        stable = finish_provenance(manifest, args.repo)
        manifest["finished_at_utc"] = datetime.now(UTC).isoformat()
        if not stable and manifest["execution_status"] == "completed":
            manifest.update(execution_status="failed", failure_type="ProvenanceChanged")
            save_checkpoint(args.output, manifest)
            raise RuntimeError("Code or benchmark inputs changed during the run")
        save_checkpoint(args.output, manifest)


def main() -> None:
    """Require an explicit output and isolated Redis environment; never provision a service."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--blocks", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--cycles", type=int, choices=range(1, 11), default=10)
    execute(parser.parse_args())


if __name__ == "__main__":
    main()
