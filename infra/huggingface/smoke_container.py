"""Exercise the built synthetic container over HTTP without invoking a real provider."""

import argparse
import json
import time
from urllib.parse import urlparse

import httpx


def require(condition: bool, message: str) -> None:
    """Keep smoke failures active under optimized Python, unlike removable assertions."""
    if not condition:
        raise RuntimeError(message)


def wait_for_startup(client: httpx.Client) -> None:
    """Bound cold-start readiness checks so CI never waits indefinitely for a broken image."""
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            response = client.get("/ready")
            if response.status_code == 200 and response.json().get("status") == "ready":
                return
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(0.5)
    raise RuntimeError("The demo container did not become ready within 45 seconds")


def check_demo(base_url: str) -> dict[str, str | int | bool]:
    """Verify UI, synthetic scope, a cited financial result, and a denied borrower request."""
    parsed = urlparse(base_url)
    require(
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"},
        "Container smoke tests require an explicit loopback HTTP address",
    )
    with httpx.Client(base_url=base_url, timeout=2, follow_redirects=False) as client:
        wait_for_startup(client)
        health = client.get("/health")
        require(health.status_code == 200 and health.json()["status"] == "ok", "Health failed")
        web = client.get("/")
        require(web.status_code == 200 and "Underwriting workspace" in web.text, "UI missing")
        borrowers = client.get("/api/v1/borrowers")
        require(borrowers.status_code == 200, "Borrower list failed")
        scope = borrowers.json()
        require(scope["mode"] == "demo" and len(scope["borrowers"]) == 5, "Unexpected demo scope")
        request = {
            "borrower_id": "borrower-001",
            "effective_at": "2026-01-01",
            "question": "What is the debt service coverage ratio?",
        }
        response = client.post("/api/v1/query", json=request)
        require(response.status_code == 200, "Query failed")
        require("no-store" in response.headers.get("cache-control", ""), "API caching allowed")
        packet = response.json()
        require(packet["provider_mode"] == "local-extractive", "Unexpected provider")
        require(packet["calculated_metrics"][0]["value"] == "1.5000", "Unexpected DSCR")
        chunk = packet["evidence"][0]
        source = client.get(
            f"/api/v1/evidence/{chunk['chunk_id']}",
            params={"borrower_id": request["borrower_id"], "effective_at": request["effective_at"]},
        )
        require(source.status_code == 200, "Source inspection failed")
        require(source.json()["content_hash"] == chunk["content_hash"], "Source provenance changed")
        request["borrower_id"] = "borrower-9999"
        denied = client.post("/api/v1/query", json=request)
        require(denied.status_code == 403, "Unauthorized borrower request was not denied")
        return {
            "status": "pass",
            "mode": scope["mode"],
            "authorized_borrowers": 5,
            "provider": packet["provider_mode"],
            "dscr": "1.5000",
            "source_matches": True,
            "unauthorized_borrower_status": denied.status_code,
        }


def main() -> None:
    """An explicit local address makes the smoke target reviewable in shell and CI output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args()
    print(json.dumps(check_demo(args.base_url), indent=2))


if __name__ == "__main__":
    main()
