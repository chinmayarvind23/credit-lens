"""Feed a local dashboard from actual synthetic HTTP work, never fabricated counters."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

import httpx

from creditlens.settings import Settings
from scripts.benchmark_cache_http import REQUESTS, serve


def run(output: Path, seconds: int) -> None:
    """Use explicit demo settings and a fresh database; expose only aggregate metrics."""
    output = output.resolve()
    if output.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("Evidence must live outside the repository")
    output.mkdir(parents=True, exist_ok=False)
    settings = Settings(
        _env_file=None,
        mode="demo",
        database_url=f"sqlite:///{(output / 'audit.db').as_posix()}",
        telemetry_enabled=True,
        response_cache_enabled=True,
        trace_file=str(output / "traces.jsonl"),
    )
    with serve(settings) as (origin, app):

        class MetricsHandler(BaseHTTPRequestHandler):
            """A metrics-only bridge allows Docker to scrape the isolated public demo."""

            def do_GET(self) -> None:
                """Reject all other paths; no headers, inputs or files enter the response."""
                if self.path != "/metrics":
                    self.send_error(404)
                    return
                body = app.state.telemetry.render()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                """Avoid retaining caller addresses or arbitrary paths in fixture logs."""

        bridge = ThreadingHTTPServer(("0.0.0.0", 19101), MetricsHandler)  # noqa: S104
        thread = Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        try:
            with (
                httpx.Client(base_url=origin, timeout=10, trust_env=False) as client,
                (output / "responses.jsonl").open("w", encoding="utf-8") as stream,
            ):
                deadline = monotonic() + seconds
                index = 0
                print("Synthetic metrics ready on port 19101", flush=True)
                while monotonic() < deadline:
                    response = client.post("/api/v1/query", json=REQUESTS[index % 5])
                    response.raise_for_status()
                    stream.write(json.dumps(response.json()) + "\n")
                    stream.flush()
                    if index % 5 == 0:
                        denied = client.post("/api/v1/query", json={})
                        if denied.status_code != 422:
                            raise RuntimeError("Malformed query did not fail validation")
                    index += 1
                    sleep(2)
        finally:
            (output / "metrics.txt").write_bytes(app.state.telemetry.render())
            bridge.shutdown()
            bridge.server_close()
            thread.join(timeout=5)


def main() -> None:
    """Bound workload duration and keep the normal application quota in force."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds", type=int, choices=range(30, 1801), default=300)
    args = parser.parse_args()
    run(args.output, args.seconds)


if __name__ == "__main__":
    main()
