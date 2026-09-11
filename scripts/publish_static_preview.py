"""Publish a verified free Static Space and record its immutable public file hashes."""

import argparse
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx

from scripts.build_static_preview import MANIFEST, verify_static


def http_status(error: Exception) -> int | None:
    """Read SDK and raw HTTP status without depending on optional SDK exception classes."""
    response = getattr(error, "response", None)
    return response.status_code if isinstance(response, httpx.Response) else None


def free_static(api: Any, repo_id: str) -> Any:
    """Reject private or compute-backed destinations without mutating their settings."""
    info = api.space_info(repo_id)
    runtime = api.get_space_runtime(repo_id)
    if info.sdk != "static" or info.private or runtime.hardware or runtime.requested_hardware:
        raise ValueError("Only a public Static destination without compute is permitted")
    return info


def verify_remote(
    api: Any,
    client: httpx.Client,
    repo_id: str,
    revision: str,
    files: dict[str, str],
    initial: set[str],
) -> list[str]:
    """Verify the immutable uploaded tree and byte hashes through unauthenticated HTTP."""
    inventory = set(api.list_repo_files(repo_id, repo_type="space", revision=revision))
    if inventory != set(files) | (initial & {".gitattributes"}):
        raise ValueError("Published inventory differs from the reviewed artifact")
    for name, expected in files.items():
        response = client.get(f"https://huggingface.co/spaces/{repo_id}/resolve/{revision}/{name}")
        response.raise_for_status()
        if sha256(response.content).hexdigest() != expected:
            raise ValueError(f"Published bytes differ: {name}")
    free_static(api, repo_id)
    return sorted(inventory)


def publish(
    api: Any, client: httpx.Client, repo_id: str, stage: Path, audit: Path
) -> dict[str, Any]:
    """Use current login, refuse unexpected remote files, and never request compute or storage."""
    manifest = verify_static(stage)
    if manifest.get("ui_source_tree_dirty") is not False:
        raise ValueError("Publication requires a clean reviewed UI snapshot")
    if audit.exists() or audit.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError("Publication audit must be new and outside the code repository")
    account = api.whoami()["name"]
    if repo_id.count("/") != 1 or repo_id.split("/")[0] != account:
        raise ValueError("Destination must explicitly belong to the current HF account")
    files = {**manifest["files"], MANIFEST: sha256((stage / MANIFEST).read_bytes()).hexdigest()}
    result: dict[str, Any] = {
        "status": "started",
        "repo_id": repo_id,
        "sdk": "static",
        "started_at": datetime.now(UTC).isoformat(),
        "files": files,
        "no_compute_or_storage_requested": True,
    }
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(json.dumps(result, indent=2), encoding="utf-8")
    try:
        try:
            info = api.space_info(repo_id)
        except httpx.HTTPError as error:
            if http_status(error) != 404:
                raise
            api.create_repo(repo_id=repo_id, repo_type="space", space_sdk="static", private=False)
            result["created"] = True
            audit.write_text(json.dumps(result, indent=2), encoding="utf-8")
            info = api.space_info(repo_id)
        info = free_static(api, repo_id)
        result["hardware"] = None
        result["requested_hardware"] = None
        initial = set(api.list_repo_files(repo_id, repo_type="space", revision=info.sha))
        result["initial_inventory"] = sorted(initial)
        if initial - set(files) - {".gitattributes"}:
            raise ValueError("Unexpected remote files require inspection before publication")
        # Recheck local bytes immediately before the single bounded remote write.
        if verify_static(stage) != manifest:
            raise ValueError("Reviewed stage changed before upload")
        commit = api.upload_folder(
            repo_id=repo_id,
            repo_type="space",
            folder_path=stage,
            allow_patterns=sorted(files),
            parent_commit=info.sha,
            commit_message="Publish recorded synthetic CreditLens preview",
        )
        result["revision"] = commit.oid
        audit.write_text(json.dumps(result, indent=2), encoding="utf-8")
        inventory = verify_remote(api, client, repo_id, commit.oid, files, initial)
        result.update(
            status="verified",
            inventory=sorted(inventory),
            space_url=f"https://huggingface.co/spaces/{repo_id}",
        )
    except Exception as error:
        result.update(status="failed", error_type=type(error).__name__)
        if http_status(error) is not None:
            result["http_status"] = http_status(error)
        raise
    finally:
        result["finished_at"] = datetime.now(UTC).isoformat()
        audit.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    """Require an explicit account destination and use only cached HF credentials."""
    from huggingface_hub import HfApi, set_client_factory

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--ipv4", action="store_true")
    args = parser.parse_args()

    def client_factory() -> httpx.Client:
        """Optional IPv4 binds around local TLS resets without disabling certificate checks."""
        # This is an outbound client source address, not a listening server.
        transport = httpx.HTTPTransport(local_address="0.0.0.0" if args.ipv4 else None, retries=2)  # noqa: S104
        return httpx.Client(transport=transport, timeout=60, follow_redirects=True)

    set_client_factory(client_factory)
    with client_factory() as client:
        print(json.dumps(publish(HfApi(), client, args.repo_id, args.stage, args.audit), indent=2))


if __name__ == "__main__":
    main()
