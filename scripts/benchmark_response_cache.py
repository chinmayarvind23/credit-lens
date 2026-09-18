"""Compare full HTTP responses with cache off/on, fresh disk audits and actual local telemetry."""

import argparse
import json
from hashlib import sha256
from pathlib import Path
from time import perf_counter

import httpx
from sqlalchemy import select

from creditlens.domain import Packet
from creditlens.evaluation import percentile, source_hashes
from creditlens.limits import QueryLimiter
from creditlens.settings import Settings
from creditlens.storage import audit_events
from scripts.benchmark_cache_http import REQUESTS, fingerprint, serve


def measure(output: Path, enabled: bool, block: int, baseline: dict) -> list[dict]:
    """Alternate modes across blocks, keeping telemetry and durable audit enabled in both."""
    name = f"{'cached' if enabled else 'uncached'}-{block}"
    settings = Settings(
        database_url=f"sqlite:///{(output / (name + '.db')).as_posix()}",
        response_cache_enabled=enabled,
        telemetry_enabled=True,
        trace_file=str(output / (name + "-traces.jsonl")),
    )
    rows = []
    with serve(settings) as (origin, app):
        app.state.limiter = QueryLimiter(limit=1000)
        with httpx.Client(base_url=origin, timeout=10, trust_env=False) as client:
            for cycle in range(11):
                for body in REQUESTS:
                    started = perf_counter()
                    response = client.post("/api/v1/query", json=body)
                    elapsed = (perf_counter() - started) * 1000
                    response.raise_for_status()
                    packet = Packet.model_validate(response.json())
                    key = body["borrower_id"]
                    baseline.setdefault(key, fingerprint(packet))
                    if fingerprint(packet) != baseline[key] or packet.cache_hit != (
                        enabled and cycle > 0
                    ):
                        raise ValueError(
                            "Cached result or hit state differs from the uncached workflow"
                        )
                    rows.append(
                        {
                            "mode": "cached" if enabled else "uncached",
                            "block": block,
                            "warmup": cycle == 0,
                            "http_ms": elapsed,
                            "request_id": packet.request_id,
                            "cache_hit": packet.cache_hit,
                            "packet": packet.model_dump(mode="json"),
                        }
                    )
        with app.state.store.engine.connect() as connection:
            audits = connection.execute(select(audit_events.c.request_id)).scalars().all()
        if set(audits) != {r["request_id"] for r in rows} or len(audits) != len(rows):
            raise ValueError("Every delivered packet requires a distinct acknowledged audit")
        (output / (name + "-metrics.txt")).write_bytes(app.state.telemetry.render())
    return rows


def run(output: Path) -> None:
    """Record 150 measured requests per mode with packets, source hashes and gates."""
    repo = Path(__file__).resolve().parents[1]
    output = output.resolve()
    if output.exists() or output.is_relative_to(repo):
        raise ValueError("Choose a fresh private directory outside the source repository")
    output.mkdir(parents=True)
    before = source_hashes(repo)
    rows: list[dict] = []
    baseline: dict = {}
    for block in range(3):
        for enabled in (False, True) if block % 2 == 0 else (True, False):
            rows.extend(measure(output, enabled, block, baseline))
    summary = {}
    for mode in ("uncached", "cached"):
        measured = [r for r in rows if r["mode"] == mode and not r["warmup"]]
        summary[mode] = {
            "requests": len(measured),
            "p50_http_ms": percentile([r["http_ms"] for r in measured], 0.5),
            "p95_http_ms": percentile([r["http_ms"] for r in measured], 0.95),
            "cache_hits": sum(r["cache_hit"] for r in measured),
        }
    stable = before == source_hashes(repo)
    latency_pass = summary["cached"]["p95_http_ms"] <= 2700
    unique = len({r["request_id"] for r in rows}) == len(rows)
    report = {
        "status": "passed" if stable and unique and latency_pass else "failed",
        "latency_gate_ms": 2700,
        "latency_gate_passed": latency_pass,
        "source_hashes": before,
        "source_stable": stable,
        "summary": summary,
        "audited_requests": len(rows),
        "fresh_request_ids": unique,
        "substantive_packets_equal": True,
        "p95_reduction_percent": 100
        * (1 - summary["cached"]["p95_http_ms"] / summary["uncached"]["p95_http_ms"]),
        "cost_usd": None,
        "paid_service_calls": 0,
        "concurrency": 1,
        "limits": "Serial loopback HTTP, five synthetic borrowers, 330-page fixture, "
        "lexical retrieval, shared workstation; not a production/cloud benchmark.",
    }
    (output / "records.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    report["records_sha256"] = sha256((output / "records.jsonl").read_bytes()).hexdigest()
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "source_hashes"}, indent=2))
    if report["status"] != "passed":
        raise RuntimeError("HTTP acceptance gates failed")


def main() -> None:
    """Run only owned ephemeral loopback servers and close them after each block."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args().output)


if __name__ == "__main__":
    main()
