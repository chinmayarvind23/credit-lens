"""Publish a verified browser application only to an existing free public Static Space."""

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import httpx

from scripts.build_browser_space import BROWSER, MODULES, ROOT, public_fixture
from scripts.publish_static_preview import free_static, verify_remote


def retained_demo(
    client: httpx.Client, repo_id: str, revision: str, previous: set[str]
) -> dict[str, str]:
    """Preserve the GIF at the reviewed immutable head and verify its bytes after upload."""
    if "demo.gif" not in previous:
        return {}
    response = client.get(f"https://huggingface.co/spaces/{repo_id}/resolve/{revision}/demo.gif")
    response.raise_for_status()
    if not response.content.startswith((b"GIF87a", b"GIF89a")):
        raise ValueError("Existing demo recording is not a GIF")
    return {"demo.gif": sha256(response.content).hexdigest()}


def verify_stage(stage: Path) -> dict[str, str]:
    """Reject dirty builds, altered assets and any non-allowlisted upload before HF mutation."""
    manifest = json.loads((stage / "deployment-manifest.json").read_text(encoding="utf-8"))
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()  # noqa: S603,S607 - fixed read-only argv
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()  # noqa: S603,S607 - fixed read-only argv
    if dirty or manifest["source_dirty"] or manifest["source_revision"] != revision:
        raise ValueError("Publish a clean build of the reviewed current commit")
    files = manifest["files"]
    paths = [p for p in stage.rglob("*") if p.is_file()]
    if any(p.is_symlink() for p in stage.rglob("*")):
        raise ValueError("Symlinks are not deployable assets")
    if {p.relative_to(stage).as_posix() for p in paths} != set(files) | {
        "deployment-manifest.json"
    }:
        raise ValueError("Unexpected staged files")
    for name, digest in files.items():
        if not re.fullmatch(
            r"(?:index\.html|index-[a-z0-9]+\.(?:js|css)|python-bundle\.json|worker\.js|README\.md|runtime/[a-zA-Z0-9_.-]+)",
            name,
        ):
            raise ValueError("Unexpected asset name")
        if sha256((stage / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Changed asset: {name}")
    lock = json.loads((BROWSER / "runtime-lock.json").read_text(encoding="utf-8"))
    if (
        manifest["runtime"] != lock
        or {k[8:]: v for k, v in files.items() if k.startswith("runtime/")} != lock["files"]
    ):
        raise ValueError("Runtime differs from the reviewed lock")
    bundle = json.loads((stage / "python-bundle.json").read_text(encoding="utf-8"))["files"]
    expected = {
        f"creditlens/{name}.py": (ROOT / f"src/creditlens/{name}.py").read_text(
            encoding="utf-8-sig"
        )
        for name in MODULES
    }
    expected["bridge.py"] = (BROWSER / "bridge.py").read_text(encoding="utf-8-sig")
    expected["fixture.json"] = json.dumps(public_fixture())
    if bundle != expected:
        raise ValueError("Python bundle differs from reviewed source or public synthetic fixture")
    files["deployment-manifest.json"] = sha256(
        (stage / "deployment-manifest.json").read_bytes()
    ).hexdigest()
    return files


def publish(args: argparse.Namespace) -> None:
    """Use a parent-bound commit and immutable remote hashes, never a hardware or billing API."""
    from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi, set_client_factory

    files = verify_stage(args.stage)
    if args.audit.exists() or args.audit.resolve().is_relative_to(ROOT):
        raise ValueError("Choose a fresh private audit path outside the repository")

    def client_factory() -> httpx.Client:
        """Verified TLS and IPv4 avoid this workstation's broken IPv6 route."""
        return httpx.Client(
            transport=httpx.HTTPTransport(local_address="0.0.0.0"),  # noqa: S104 - egress
            timeout=120,
            follow_redirects=True,
        )

    set_client_factory(client_factory)
    api = HfApi()
    if api.whoami()["name"] != args.repo_id.split("/")[0]:
        raise ValueError("Publish only to the authenticated owner's Space")
    info = free_static(api, args.repo_id)
    if info.sha != args.expected_revision:
        raise ValueError("Remote revision changed; inspect before publication")
    previous = set(api.list_repo_files(args.repo_id, repo_type="space", revision=info.sha))
    # Only replace this project's known entry assets; unrelated remote files require review.
    known = {
        ".gitattributes",
        "README.md",
        "index.html",
        "demo.gif",
        "deployment-manifest.json",
        "worker.js",
        "python-bundle.json",
    }
    if any(
        name not in known
        and not re.fullmatch(r"(?:index-[a-z0-9]+\.(?:js|css)|runtime/[a-zA-Z0-9_.-]+)", name)
        for name in previous
    ):
        raise ValueError("Unexpected remote inventory")
    with client_factory() as client:
        retained = retained_demo(client, args.repo_id, info.sha, previous)
    expected_files = {**files, **retained}
    report = {
        "status": "verified_before_publication",
        "repo_id": args.repo_id,
        "previous_revision": info.sha,
        "started_at": datetime.now(UTC).isoformat(),
        "files": expected_files,
        "retained_files": retained,
        "no_paid_hardware_requested": True,
    }
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    args.audit.write_text(json.dumps(report, indent=2), encoding="utf-8")
    operations = [
        CommitOperationAdd(path_in_repo=name, path_or_fileobj=args.stage / name) for name in files
    ]
    operations.extend(
        CommitOperationDelete(path_in_repo=name)
        for name in previous - set(expected_files) - {".gitattributes"}
    )
    commit = api.create_commit(
        repo_id=args.repo_id,
        repo_type="space",
        operations=operations,
        parent_commit=info.sha,
        commit_message="docs cleanup",
    )
    report.update(status="published_pending_verification", revision=commit.oid)
    args.audit.write_text(json.dumps(report, indent=2), encoding="utf-8")
    with client_factory() as client:
        report["inventory"] = verify_remote(
            api, client, args.repo_id, commit.oid, expected_files, previous
        )
    free_static(api, args.repo_id)
    report["status"] = "verified"
    args.audit.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "revision": commit.oid, "files": len(files)}))


def main() -> None:
    """Require explicit destination and expected remote head for every free publication."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--expected-revision", required=True)
    publish(parser.parse_args())


if __name__ == "__main__":
    main()
