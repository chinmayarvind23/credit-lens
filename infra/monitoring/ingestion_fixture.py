"""Exercise local ingestion dashboards with explicitly injected synthetic queue faults."""

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import sleep
from uuid import uuid4

from prometheus_client import CollectorRegistry, generate_latest
from sqlalchemy import func, select, update

from creditlens.corpus import borrower_pages
from creditlens.domain import Principal
from creditlens.ingestion_jobs import IngestionInput, JobStore, initialize_jobs, jobs
from creditlens.ingestion_metrics import IngestionCollector
from creditlens.storage import grants, open_database


def seed(store: JobStore) -> None:
    """Use normal job APIs, then inject only timestamps to reproduce backlog and lease loss."""
    actor = Principal(
        subject="synthetic-monitor-" + uuid4().hex,
        tenant_id="demo-bank",
        role="admin",
        borrower_ids=("borrower-001",),
        acl_groups=("underwriting",),
        revision=1,
    )
    with store.engine.begin() as connection:
        connection.execute(grants.insert().values(**actor.model_dump(mode="json"), enabled=True))
    source = IngestionInput(source_sha256="a" * 64, pages=borrower_pages(1)[:2], parser="digital")
    pending = store.submit(source, actor, "pending")
    running = store.submit(source.model_copy(update={"parser": "ocr"}), actor, "expired")
    failed = store.submit(source, actor, "failed")
    if store.claim(running.job_id) is None:
        raise RuntimeError("Synthetic running job was not claimed")
    lease = store.claim(failed.job_id)
    if lease is None:
        raise RuntimeError("Synthetic failed job was not claimed")
    store.fail(lease, "invalid_source", retryable=False)
    with store.engine.begin() as connection:
        connection.execute(
            update(jobs)
            .where(jobs.c.job_id == pending.job_id)
            .values(updated_at=datetime.now(UTC) - timedelta(seconds=400))
        )
        connection.execute(
            update(jobs)
            .where(jobs.c.job_id == running.job_id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=30))
        )


def run(port: int, output: Path, seconds: int) -> None:
    """Use only a fresh local fixture; the temporary metrics endpoint contains synthetic gauges."""
    output = output.resolve()
    if output.is_relative_to(Path(__file__).resolve().parents[2]):
        raise ValueError("Store drill evidence outside the repository")
    output.mkdir(parents=True, exist_ok=False)
    engine = open_database(
        f"postgresql+psycopg://postgres:creditlens-test-only@127.0.0.1:{port}/creditlens_test"
    )
    try:
        initialize_jobs(engine)
        with engine.connect() as connection:
            if connection.scalar(select(func.count()).select_from(jobs)):
                raise ValueError("Use a fresh owned fixture with no ingestion jobs")
        store = JobStore(engine, "synthetic-monitor-" + uuid4().hex)
        seed(store)
        registry = CollectorRegistry()
        registry.register(IngestionCollector(store))

        class Handler(BaseHTTPRequestHandler):
            """Expose only the synthetic aggregate scrape, with no directory or file serving."""

            def do_GET(self) -> None:
                """Current SQL state is read on every scrape rather than replaying a metric file."""
                if self.path != "/metrics":
                    self.send_error(404)
                    return
                body = generate_latest(registry)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                """Do not retain caller addresses or arbitrary request text."""

        server = ThreadingHTTPServer(("0.0.0.0", 19101), Handler)  # noqa: S104 - synthetic drill
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            snapshot = {
                "fault_injection": "aged queued state and expired OCR lease",
                "jobs": [asdict(row) for row in store.aggregates()],
            }
            (output / "snapshot.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
            print("Synthetic ingestion metrics ready on port 19101", flush=True)
            sleep(seconds)
            (output / "metrics.txt").write_bytes(generate_latest(registry))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    finally:
        engine.dispose()


def main() -> None:
    """Bound the drill lifetime and require an explicit owned database port and fresh output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, choices=range(30, 301), default=120)
    args = parser.parse_args()
    run(args.port, args.output, args.seconds)


if __name__ == "__main__":
    main()
