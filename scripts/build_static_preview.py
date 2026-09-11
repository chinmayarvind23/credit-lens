"""Build and verify a precompiled Static Space with an explicit public artifact inventory."""

import argparse
import json
import re
import shutil
from hashlib import sha256
from pathlib import Path
from typing import Any

from scripts.capture_preview import validate_capture
from scripts.deploy_space import checked_bytes, run

MANIFEST = "preview-manifest.json"


def static_inventory(files: set[str]) -> None:
    """Allow one HTML entry, one JS bundle, one CSS file, and reviewed data/card."""
    required = {"index.html", "README.md", "recordings.json"}
    bundles = files - required
    if not required <= files or len(bundles) != 2:
        raise ValueError("Unexpected static preview inventory")
    if not all(re.fullmatch(r"preview-[a-z0-9]+\.(js|css)", name) for name in bundles):
        raise ValueError("Unexpected static asset name")
    if {Path(name).suffix for name in bundles} != {".js", ".css"}:
        raise ValueError("Static preview requires JavaScript and CSS")


def verify_static(stage: Path) -> dict[str, Any]:
    """Verify exact inventory, bytes, and a static-only card before upload."""
    manifest: dict[str, Any] = json.loads(checked_bytes(stage / MANIFEST, stage))
    if manifest.get("mode") != "recorded-synthetic-preview":
        raise ValueError("Unexpected preview mode")
    files = manifest["files"]
    static_inventory(set(files))
    inventory = {path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()}
    if inventory != set(files) | {MANIFEST}:
        raise ValueError("Static preview has missing or unexpected files")
    for name, digest in files.items():
        if sha256(checked_bytes(stage / name, stage)).hexdigest() != digest:
            raise ValueError(f"Static preview file changed: {name}")
    card = (stage / "README.md").read_text(encoding="utf-8")
    lines = card.split("---", 2)
    if len(lines) != 3 or lines[0].strip():
        raise ValueError("Static Space front matter is required")
    fields = [line.split(":", 1) for line in lines[1].splitlines() if line.strip()]
    allowed = {"title", "emoji", "colorFrom", "colorTo", "sdk", "app_file", "pinned"}
    if any(len(pair) != 2 or pair[0] not in allowed for pair in fields):
        raise ValueError("Unexpected Static Space configuration")
    settings = {key: value.strip() for key, value in fields}
    if len(settings) != len(fields):
        raise ValueError("Duplicate Static Space configuration")
    if settings.get("sdk") != "static" or settings.get("app_file") != "index.html":
        raise ValueError("Static Space card is required")
    validate_capture(json.loads((stage / "recordings.json").read_text(encoding="utf-8")))
    return manifest


def build(captures: Path, output: Path) -> dict[str, Any]:
    """Compile reviewed UI source and copy only the explicit public capture and static card."""
    repo = Path(__file__).resolve().parents[1]
    if output.exists() or output.resolve().is_relative_to(repo):
        raise ValueError("Static stage must be new and outside the code repository")
    payload = checked_bytes(captures, captures.parent)
    recordings = validate_capture(json.loads(payload))
    bun = shutil.which("bun")
    if bun is None or run([bun, "--version"]) != "1.3.10":
        raise ValueError("The reviewed Bun 1.3.10 toolchain must be on PATH")
    output = output.resolve()
    run([bun, "run", "typecheck"], cwd=repo / "apps/web")
    run(
        [bun, "build", "./preview.html", "--minify", "--outdir", str(output)], cwd=repo / "apps/web"
    )
    (output / "preview.html").rename(output / "index.html")
    (output / "recordings.json").write_bytes(payload)
    card = repo / "infra/huggingface/static/README.md"
    (output / "README.md").write_bytes(checked_bytes(card, repo))
    files = {
        path.name: sha256(checked_bytes(path, output)).hexdigest() for path in output.iterdir()
    }
    static_inventory(set(files))
    manifest = {
        "mode": "recorded-synthetic-preview",
        "ui_source_revision": run(["git", "rev-parse", "HEAD"], cwd=repo),
        "ui_source_tree_dirty": bool(
            run(
                [
                    "git",
                    "status",
                    "--porcelain",
                    "--",
                    "apps/web",
                    "scripts/build_static_preview.py",
                    "scripts/capture_preview.py",
                    "scripts/deploy_space.py",
                    "scripts/__init__.py",
                    "infra/huggingface/static",
                ],
                cwd=repo,
            )
        ),
        "api_source_revision": recordings["source_revision"],
        "files": files,
    }
    (output / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return verify_static(output)


def main() -> None:
    """Keep stage generation and verification explicit without cloud writes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "verify"))
    parser.add_argument("--captures", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "build" and args.captures is None:
        parser.error("--captures is required when building")
    result = (
        build(args.captures, args.output) if args.action == "build" else verify_static(args.output)
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
