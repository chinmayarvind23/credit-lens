"""Serve one verified evaluation snapshot without exposing its private source directory."""

import argparse
import json
import time
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socket import socket


class SnapshotServer(HTTPServer):
    """Bound accepted sockets so incomplete local requests cannot hold the fixture open."""

    def get_request(self) -> tuple[socket, tuple[str, int]]:
        """Apply the scrape deadline to both request reads and response writes."""
        connection, address = super().get_request()
        connection.settimeout(3)
        return connection, address


def payload(directory: Path) -> bytes:
    """Pin the exported bytes so edits after startup cannot alter the scrape result."""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    data = (directory / "metrics.prom").read_bytes()
    if sha256(data).hexdigest() != manifest["metrics_sha256"]:
        raise ValueError("Monitoring snapshot hash changed")
    return data


def serve(data: bytes, host: str, port: int, seconds: int) -> None:
    """Expose only aggregate metric bytes for a bounded local verification window."""

    class Handler(BaseHTTPRequestHandler):
        """No directory listing, paths, query logging or private artifacts are served."""

        def do_GET(self) -> None:
            """Prometheus receives the frozen export only at the exact metrics route."""
            if self.path != "/metrics":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            """Request URLs and peer addresses are unnecessary for aggregate verification."""

    with SnapshotServer((host, port), Handler) as server:
        server.timeout = 0.5
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            server.handle_request()


def main() -> None:
    """Default to loopback; an explicit host permits an owned Docker scraper."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19102)
    parser.add_argument("--seconds", type=int, choices=range(1, 3601), default=180)
    args = parser.parse_args()
    serve(payload(args.directory), args.host, args.port, args.seconds)


if __name__ == "__main__":
    main()
