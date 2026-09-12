"""Measure concurrent HTTP across separate processes and verify shared authority after load."""

import argparse
import json
import multiprocessing
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx
from sqlalchemy import select, update
from sqlalchemy.engine import make_url

from creditlens.domain import Packet
from creditlens.evaluation import git_metadata, percentile, source_hashes
from creditlens.settings import Settings
from creditlens.sql_catalog import SqlEvidenceCatalog
from creditlens.storage import GrantStore, audit_events, grants, open_database
from scripts.benchmark_cache_http import REQUESTS, fingerprint, serve


def child_server(url, catalog_id, directory, connection, stop):
    """Keep inherited deployment settings out of this owned synthetic child process."""
    for key in list(os.environ):
        if key.startswith("CREDITLENS_"):
            del os.environ[key]
    config = Settings(
        database_url=url,
        catalog_backend="postgres",
        demo_catalog_id=catalog_id,
        response_cache_enabled=True,
        telemetry_enabled=True,
        trace_file=str(directory / "traces.jsonl"),
    )
    with serve(config, startup_timeout=60) as (origin, app):
        connection.send({"origin": origin, "pid": os.getpid()})
        connection.close()
        stop.wait(180)
        (directory / "metrics.txt").write_bytes(app.state.telemetry.render())


@contextmanager
def replica(url, catalog_id, directory):
    """Join or terminate only the exact child created here; never inspect unrelated servers."""
    directory.mkdir()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    stop = context.Event()
    process = context.Process(target=child_server, args=(url, catalog_id, directory, child, stop))
    process.start()
    child.close()
    try:
        if not parent.poll(75):
            raise RuntimeError("Owned replica did not start")
        yield parent.recv()
    finally:
        parent.close()
        stop.set()
        process.join(20)
        if process.is_alive():
            process.terminate()
            process.join(10)
        if process.is_alive():
            process.kill()
            process.join(10)
        if process.exitcode != 0:
            raise RuntimeError("Owned replica failed or required forced cleanup")


def query(client, body, replica_id, phase):
    """Retain errors alongside successful packets so overload cannot disappear from reports."""
    started = perf_counter()
    response = client.post("/api/v1/query", json=body)
    row = {
        "phase": phase,
        "replica": replica_id,
        "borrower_id": body["borrower_id"],
        "status": response.status_code,
        "http_ms": (perf_counter() - started) * 1000,
        "body": response.json(),
    }
    if response.headers.get("cache-control") != "no-store":
        raise ValueError("Private response header missing")
    return row


def authority_checks(engine, catalog_id, clients, rows):
    """Commit page and grant revocation and observe both real processes without restart."""
    packet = Packet.model_validate(rows[0]["body"])
    target = packet.evidence[0]
    SqlEvidenceCatalog(engine, catalog_id).revoke(target.chunk_id)
    for i, client in enumerate(clients):
        row = query(client, REQUESTS[0], i, "page_revoked")
        rows.append(row)
        if row["status"] != 200:
            raise ValueError("Post-revocation query failed")
        current = Packet.model_validate(row["body"])
        if current.cache_hit or any(
            (c.document_id, c.document_version, c.page)
            == (target.document_id, target.document_version, target.page)
            for c in current.evidence
        ):
            raise ValueError("Revoked page or stale cache survived publication")
    with engine.begin() as connection:
        connection.execute(
            update(grants)
            .where(grants.c.subject == "synthetic-demo")
            .values(enabled=False, revision=grants.c.revision + 1)
        )
    for i, client in enumerate(clients):
        row = query(client, REQUESTS[0], i, "grant_revoked")
        rows.append(row)
        if row["status"] != 403 or row["body"].get("error", {}).get("code") != "access_denied":
            raise ValueError("Revoked grant was not denied")
    with engine.begin() as connection:
        connection.execute(
            update(grants)
            .where(grants.c.subject == "synthetic-demo")
            .values(enabled=True, revision=grants.c.revision + 1)
        )
    for i, client in enumerate(clients):
        burst = [query(client, REQUESTS[0], i, "overload") for _ in range(8)]
        rows.extend(burst)
        if not any(r["status"] == 429 for r in burst) or any(
            r["status"] not in {200, 429} for r in burst
        ):
            raise ValueError("Default quota did not bound the overload burst")


def phase(url, directory, concurrency, engine):
    """Use unchanged default quotas and shared SQL with fresh process-local caches per phase."""
    catalog_id = "synthetic-load-" + uuid4().hex
    rows = []
    with engine.connect() as connection:
        prior = set(connection.execute(select(audit_events.c.request_id)).scalars())
    try:
        with ExitStack() as stack:
            replicas = [
                stack.enter_context(replica(url, catalog_id, directory / str(i))) for i in range(2)
            ]
            if replicas[0]["pid"] == replicas[1]["pid"]:
                raise ValueError("Benchmark requires separate processes")
            clients = [
                stack.enter_context(httpx.Client(base_url=r["origin"], timeout=15, trust_env=False))
                for r in replicas
            ]
            baseline = {}
            for i, client in enumerate(clients):
                for body in REQUESTS:
                    row = query(client, body, i, "warmup")
                    rows.append(row)
                    packet = Packet.model_validate(row["body"])
                    key = body["borrower_id"]
                    baseline.setdefault(key, fingerprint(packet))
                    if baseline[key] != fingerprint(packet):
                        raise ValueError("Replica warmup packets differ")
            started = perf_counter()
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [
                    pool.submit(query, clients[i % 2], REQUESTS[i % 5], i % 2, "load")
                    for i in range(100)
                ]
                for future in futures:
                    rows.append(future.result())
            elapsed = perf_counter() - started
            measured = [r for r in rows if r["phase"] == "load"]
            for row in measured:
                packet = Packet.model_validate(row["body"])
                if row["status"] != 200 or fingerprint(packet) != baseline[row["borrower_id"]]:
                    raise ValueError("Concurrent output differs from serial baseline")
            authority_checks(engine, catalog_id, clients, rows)
        with engine.connect() as connection:
            new_audits = (
                set(connection.execute(select(audit_events.c.request_id)).scalars()) - prior
            )
        ids = [r["body"]["request_id"] for r in rows if r["status"] == 200]
        if len(set(ids)) != len(ids) or new_audits != set(ids):
            raise ValueError("Successful responses and fresh SQL audits differ")
        p95 = percentile([r["http_ms"] for r in measured], 0.95)
        return {
            "concurrency": concurrency,
            "replicas": replicas,
            "requests": len(measured),
            "p95_http_ms": p95,
            "requests_per_second": len(measured) / elapsed,
            "load_seconds": elapsed,
            "audited_successes": len(ids),
            "statuses": dict(Counter(r["status"] for r in rows)),
            "page_revocation_both": True,
            "grant_revocation_both": True,
            "packet_equality": True,
            "latency_gate_passed": p95 <= 2700,
        }
    finally:
        (directory / "records.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )


def run(url, output):
    """Reject remote or nonempty databases before any fixture grants or pages are written."""
    parsed = make_url(url)
    if (
        parsed.drivername != "postgresql+psycopg"
        or parsed.host != "127.0.0.1"
        or (parsed.database != "creditlens_test")
    ):
        raise ValueError("Use only the owned loopback creditlens_test PostgreSQL fixture")
    repo = Path(__file__).resolve().parents[1]
    output = output.resolve()
    if output.exists() or output.is_relative_to(repo):
        raise ValueError("Use a fresh private output directory")
    before = source_hashes(repo)
    before["scripts/benchmark_multi_instance.py"] = sha256(Path(__file__).read_bytes()).hexdigest()
    engine = open_database(url)
    output.mkdir(parents=True)
    report = {"status": "failed", "git": git_metadata(repo), "source_hashes": before, "phases": []}
    try:
        with engine.connect() as connection:
            if (
                connection.execute(select(grants.c.subject).limit(1)).first()
                or connection.execute(select(audit_events.c.request_id).limit(1)).first()
            ):
                raise ValueError("Benchmark requires an empty owned fixture")
        GrantStore(engine).seed_demo()
        for concurrency in (1, 4, 8):
            directory = output / f"concurrency-{concurrency}"
            directory.mkdir()
            report["phases"].append(phase(url, directory, concurrency, engine))
        after = source_hashes(repo)
        after["scripts/benchmark_multi_instance.py"] = sha256(
            Path(__file__).read_bytes()
        ).hexdigest()
        report.update(
            source_stable=before == after,
            latency_gate_ms=2700,
            paid_service_calls=0,
            cost_usd=None,
            limits="Local lexical synthetic workload; explicit client distribution, "
            "not a load balancer. Quotas and caches remain per process. No cloud capacity claim.",
        )
        if before == after and all(p["latency_gate_passed"] for p in report["phases"]):
            report["status"] = "passed"
    finally:
        engine.dispose()
        report["artifact_hashes"] = {
            str(p.relative_to(output)): sha256(p.read_bytes()).hexdigest()
            for p in output.rglob("*")
            if p.is_file()
        }
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in {"source_hashes", "artifact_hashes"}},
            indent=2,
        )
    )
    if report["status"] != "passed":
        raise RuntimeError("Concurrent HTTP gates failed")


def main():
    """Require explicit ownership acknowledgment; credentials remain in a local environment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--owned-empty-fixture", action="store_true", required=True)
    args = parser.parse_args()
    run(os.environ["CREDITLENS_TEST_POSTGRES_URL"], args.output)


if __name__ == "__main__":
    main()
