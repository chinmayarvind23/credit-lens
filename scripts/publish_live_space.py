"""Replace a known recorded preview with an explicitly verified live synthetic workbench."""

import argparse
import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx

from scripts.publish_static_preview import free_static, verify_remote


def live_html(origin: str, template: Path) -> bytes:
    """Accept only an exact HTTPS Quick Tunnel origin, without query, path or HTML injection."""
    if not re.fullmatch(r"https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com", origin):
        raise ValueError("Expected an exact HTTPS Quick Tunnel origin")
    return template.read_text(encoding="utf-8").replace("__LIVE_ORIGIN__", origin).encode()


def verify_live(client: httpx.Client, origin: str) -> dict[str, Any]:
    """Verify fresh request IDs and computed exception results through the public live route."""
    ready = client.get(origin + "/ready")
    ready.raise_for_status()
    if ready.json() != {"status": "ready", "mode": "demo", "search": "local-extractive"}:
        raise ValueError("The live origin must advertise the reviewed synthetic workflow")
    body = {
        "borrower_id": "borrower-002",
        "question": "What is the debt service coverage and is an exception needed?",
        "effective_at": "2026-06-01",
    }
    packets = []
    for _ in range(2):
        response = client.post(origin + "/api/v1/query", json=body)
        response.raise_for_status()
        packet = response.json()
        if packet["policy_disposition"] != "EXCEPTION_REQUIRED" or not packet["evidence"]:
            raise ValueError("Live calculation verification failed")
        if any(c["tenant_id"] != "demo-bank" for c in packet["evidence"]):
            raise ValueError("Live route exposed non-demo evidence")
        packets.append(packet)
    if packets[0]["request_id"] == packets[1]["request_id"]:
        raise ValueError("Live requests must have distinct audited identities")
    return {"ready": ready.json(), "fresh_request_ids": [p["request_id"] for p in packets]}


def publish(args: argparse.Namespace, api: Any, client: httpx.Client) -> dict[str, Any]:
    """Replace only exact previously verified project files with one parent-bound HF commit."""
    from huggingface_hub import CommitOperationAdd, CommitOperationDelete

    repo = Path(__file__).resolve().parents[1]
    html = live_html(args.origin, repo / "infra/huggingface/live/index.html")
    card = (repo / "infra/huggingface/live/README.md").read_bytes()
    previous = json.loads(args.previous_audit.read_text(encoding="utf-8"))
    if previous["status"] != "verified" or previous["repo_id"] != args.repo_id:
        raise ValueError("A verified previous publication is required")
    if args.audit.exists() or args.audit.resolve().is_relative_to(repo):
        raise ValueError("Audit must be new and outside the source repository")
    if args.repo_id.split("/")[0] != api.whoami()["name"]:
        raise ValueError("Destination must belong to the authenticated account")
    info = free_static(api, args.repo_id)
    if info.sha != previous["revision"]:
        raise ValueError("Remote revision changed; inspect it before replacing files")
    inventory = set(api.list_repo_files(args.repo_id, repo_type="space", revision=info.sha))
    if inventory != set(previous["inventory"]):
        raise ValueError("Remote inventory changed")
    for name, expected in previous["files"].items():
        response = client.get(
            f"https://huggingface.co/spaces/{args.repo_id}/resolve/{info.sha}/{name}"
        )
        response.raise_for_status()
        if sha256(response.content).hexdigest() != expected:
            raise ValueError("Remote source differs from the reviewed previous upload")
    result = {
        "status": "verified_before_publication",
        "origin": args.origin,
        "repo_id": args.repo_id,
        "previous_revision": info.sha,
        "started_at": datetime.now(UTC).isoformat(),
        "live_verification": verify_live(client, args.origin),
        "files": {"index.html": sha256(html).hexdigest(), "README.md": sha256(card).hexdigest()},
        "no_paid_hardware_requested": True,
    }
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    args.audit.write_text(json.dumps(result, indent=2), encoding="utf-8")
    operations = [
        CommitOperationAdd(path_in_repo="index.html", path_or_fileobj=html),
        CommitOperationAdd(path_in_repo="README.md", path_or_fileobj=card),
    ] + [
        CommitOperationDelete(path_in_repo=name)
        for name in sorted(inventory - {"index.html", "README.md", ".gitattributes"})
    ]
    try:
        commit = api.create_commit(
            repo_id=args.repo_id,
            repo_type="space",
            parent_commit=info.sha,
            operations=operations,
            commit_message="Replace recorded examples with the live synthetic workbench",
        )
        result.update(status="uploaded", revision=commit.oid)
        args.audit.write_text(json.dumps(result, indent=2), encoding="utf-8")
        final = verify_remote(
            api, client, args.repo_id, commit.oid, result["files"], {".gitattributes"}
        )
        result.update(status="verified", inventory=sorted(final))
    except Exception as error:
        result.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        result["finished_at"] = datetime.now(UTC).isoformat()
        args.audit.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    """Use existing account credentials and an already running, explicitly named live origin."""
    from huggingface_hub import HfApi, set_client_factory

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--previous-audit", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args()

    def factory() -> httpx.Client:
        """Keep verified TLS with the IPv4 source binding required by this workstation."""
        return httpx.Client(
            transport=httpx.HTTPTransport(local_address="0.0.0.0", retries=2),  # noqa: S104
            follow_redirects=True,
            timeout=60,
        )

    set_client_factory(factory)
    with factory() as client:
        print(json.dumps(publish(args, HfApi(), client), indent=2))


if __name__ == "__main__":
    main()
