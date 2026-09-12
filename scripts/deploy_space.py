"""Stage an explicit synthetic demo allowlist, then upload an unchanged package to a named Space."""

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

FILES = (
    "Dockerfile",
    ".dockerignore",
    "pyproject.toml",
    "uv.lock",
    "apps/web/package.json",
    "apps/web/bun.lock",
    "apps/web/index.html",
    "apps/web/tsconfig.json",
    "apps/web/src/api.ts",
    "apps/web/src/browser-api.ts",
    "apps/web/src/contracts.ts",
    "apps/web/src/main.ts",
    "apps/web/src/render.ts",
    "apps/web/src/styles.css",
    "src/creditlens/__init__.py",
    "src/creditlens/api.py",
    "src/creditlens/auth.py",
    "src/creditlens/access.py",
    "src/creditlens/cache.py",
    "src/creditlens/citations.py",
    "src/creditlens/corpus.py",
    "src/creditlens/domain.py",
    "src/creditlens/errors.py",
    "src/creditlens/finance.py",
    "src/creditlens/graphql_admin.py",
    "src/creditlens/hybrid_provider.py",
    "src/creditlens/ocr.py",
    "src/creditlens/ingestion.py",
    "src/creditlens/ingestion_jobs.py",
    "src/creditlens/intent.py",
    "src/creditlens/limits.py",
    "src/creditlens/local_search.py",
    "src/creditlens/model_bundle.py",
    "src/creditlens/model_manifest.json",
    "src/creditlens/neural_search.py",
    "src/creditlens/pdf_worker.py",
    "src/creditlens/query_grounding.py",
    "src/creditlens/rerank_provider.py",
    "src/creditlens/retrieval.py",
    "src/creditlens/retrieval_cache.py",
    "src/creditlens/runtime.py",
    "src/creditlens/response_cache.py",
    "src/creditlens/observability.py",
    "src/creditlens/search_provider.py",
    "src/creditlens/settings.py",
    "src/creditlens/sqs_queue.py",
    "src/creditlens/sql_catalog.py",
    "src/creditlens/storage.py",
    "src/creditlens/workflow.py",
    "infra/huggingface/demo_entrypoint.py",
    "infra/retrieval/requirements-cpu.lock",
)
CARD = "infra/huggingface/README.md"
MANIFEST = "deployment-manifest.json"
TARGETS = frozenset((*FILES, "README.md"))
SECRET_PATTERNS = (
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    rb"\bhf_[A-Za-z0-9]{20,}\b",
    rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b",
    rb"\beyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\b",
)


def run(command: list[str], cwd: Path | None = None) -> str:
    """Use literal argv and suppress failed output that may contain secrets."""
    result = subprocess.run(  # noqa: S603 - callers construct explicit argv, never shell strings
        command, cwd=cwd, check=False, capture_output=True, text=True, timeout=300
    )
    if result.returncode:
        raise RuntimeError(f"{Path(command[0]).name} command failed with exit {result.returncode}")
    return result.stdout.strip()


def checked_bytes(path: Path, root: Path) -> bytes:
    """Reject path escapes, binary payloads, oversized files, and credential formats."""
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Unsafe source path: {path.name}")
    for parent in path.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError(f"Symlinked source directory: {parent.name}")
    payload = path.read_bytes()
    if len(payload) > 8_000_000 or b"\x00" in payload:
        raise ValueError(f"Unexpected file content: {path.name}")
    payload.decode("utf-8-sig")
    if any(re.search(pattern, payload) for pattern in SECRET_PATTERNS):
        raise ValueError(f"Possible credential in allowlisted file: {path.name}")
    return payload


def stage_package(repo: Path, output: Path) -> dict[str, Any]:
    """Copy a fresh snapshot outside the source tree; source hashes make later edits detectable."""
    repo = repo.resolve()
    output = output.resolve()
    if output.is_relative_to(repo) or repo.is_relative_to(output):
        raise ValueError("The staging directory must be outside the repository and its parents")
    if output.exists():
        raise ValueError("Use a new staging directory; existing directories are never overwritten")
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("Git is required to record source provenance")
    revision = run([git, "rev-parse", "HEAD"], repo)
    dirty = bool(run([git, "status", "--porcelain", "--", *FILES, CARD], repo))
    snapshots = {name: checked_bytes(repo / name, repo) for name in FILES}
    snapshots["README.md"] = checked_bytes(repo / CARD, repo)
    output.mkdir(parents=True)
    hashes = {}
    for name, payload in snapshots.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        hashes[name] = hashlib.sha256(payload).hexdigest()
    manifest = {
        "schema_version": 1,
        "mode": "synthetic-demo",
        "source_revision": revision,
        "source_tree_dirty": dirty,
        "files": hashes,
        "status": "staged; build and deployment require separate verification",
    }
    (output / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    verify_package(output)
    return manifest


def read_manifest(stage: Path) -> dict[str, Any]:
    """Validate snapshot metadata independently of the directory inventory and file hashes."""
    manifest: dict[str, Any] = json.loads(checked_bytes(stage / MANIFEST, stage))
    if manifest.get("schema_version") != 1 or manifest.get("mode") != "synthetic-demo":
        raise ValueError("Invalid deployment manifest")
    if not re.fullmatch(r"[a-f0-9]{40}", str(manifest.get("source_revision", ""))):
        raise ValueError("Invalid source revision in deployment manifest")
    hashes = manifest.get("files")
    if not isinstance(hashes, dict) or set(hashes) != TARGETS:
        raise ValueError("The manifest differs from the audited upload allowlist")
    return manifest


def verify_package(stage: Path) -> dict[str, Any]:
    """Require the exact allowlist and original bytes, then rescan before any external write."""
    if stage.is_symlink():
        raise ValueError("The staging directory cannot be a symlink")
    stage = stage.resolve()
    manifest = read_manifest(stage)
    hashes = manifest["files"]
    actual = set()
    for path in stage.rglob("*"):
        if path.is_symlink():
            raise ValueError("Symlinks are forbidden in staging")
        if path.is_file():
            actual.add(path.relative_to(stage).as_posix())
    if actual != TARGETS | {MANIFEST}:
        raise ValueError("Unexpected or missing files in staging")
    for name in sorted(TARGETS):
        digest = hashlib.sha256(checked_bytes(stage / name, stage)).hexdigest()
        if digest != hashes[name]:
            raise ValueError(f"Staged file changed since review: {name}")
    verify_build_inventory(stage)
    return manifest


def verify_build_inventory(stage: Path) -> None:
    """Catch drift between exact Docker file exceptions and staging, not parse Docker rules."""
    exceptions = {
        line[1:]
        for raw in (stage / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if (line := raw.strip()).startswith("!") and not line.endswith("/")
    }
    if exceptions != set(FILES):
        raise ValueError("Docker file exceptions differ from the staging allowlist")


def hf_command() -> list[str]:
    """Use the installed CLI or a pinned isolated official package without reading token files."""
    hf = shutil.which("hf")
    if hf:
        return [hf]
    uv = shutil.which("uv")
    if uv:
        return [uv, "tool", "run", "--from", "huggingface-hub==1.31.0", "hf"]
    raise RuntimeError("Install hf or place uv on PATH before uploading")


def upload_package(stage: Path, repo_id: str) -> None:
    """Upload only a verified snapshot to an existing named Space using the current CLI login."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo_id):
        raise ValueError("Use an explicit namespace/space-name destination")
    manifest = verify_package(stage)
    command = hf_command()
    run([*command, "auth", "whoami"])
    run([*command, "spaces", "info", repo_id])
    verify_package(stage)
    run(
        [
            *command,
            "upload",
            repo_id,
            str(stage.resolve()),
            ".",
            "--repo-type",
            "space",
            "--commit-message",
            f"Deploy synthetic CreditLens snapshot {manifest['source_revision'][:12]}",
        ]
    )
    print(f"Uploaded verified snapshot to https://huggingface.co/spaces/{repo_id}")
    print("Upload success does not prove a successful container build or healthy deployment.")


def main() -> None:
    """Separate staging from the explicit upload command so the package is reviewable first."""
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    staging = subcommands.add_parser("stage")
    staging.add_argument("--output", type=Path, required=True)
    verification = subcommands.add_parser("verify")
    verification.add_argument("--stage", type=Path, required=True)
    upload = subcommands.add_parser("upload")
    upload.add_argument("--stage", type=Path, required=True)
    upload.add_argument("--repo-id", required=True)
    args = parser.parse_args()
    if args.command == "stage":
        result = stage_package(Path(__file__).resolve().parents[1], args.output)
        print(json.dumps(result, indent=2))
    elif args.command == "verify":
        result = verify_package(args.stage)
        print(f"Verified {len(result['files'])} allowlisted files; no upload performed")
    else:
        upload_package(args.stage, args.repo_id)


if __name__ == "__main__":
    main()
