"""Build a self-contained free Space from the actual Python engine and public synthetic pages."""

import argparse
import json
import shutil
import subprocess
from hashlib import sha256
from pathlib import Path

import httpx

from creditlens.corpus import build_demo_borrowers, build_demo_pages

MODULES = (
    "__init__",
    "access",
    "citations",
    "domain",
    "errors",
    "finance",
    "intent",
    "retrieval",
    "search_provider",
    "storage",
    "workflow",
)
ROOT = Path(__file__).resolve().parents[1]
BROWSER = ROOT / "infra/huggingface/browser"


def public_fixture() -> dict:
    """Publish only fictional demo tenants, five borrowers and underwriting-visible policy pages."""
    borrowers = build_demo_borrowers()
    allowed = {b.borrower_id for b in borrowers}
    pages = [
        p
        for p in build_demo_pages()
        if p.tenant_id == "demo-bank"
        and p.borrower_id in allowed | {None}
        and p.acl_groups == ("underwriting",)
    ]
    return {
        "borrowers": [b.model_dump(mode="json") for b in borrowers],
        "pages": [p.model_dump(mode="json") for p in pages],
    }


def runtime_files(output: Path, cache: Path) -> None:
    """Download only digest-pinned public runtime assets; reuse verified local copies on rebuild."""
    lock = json.loads((BROWSER / "runtime-lock.json").read_text(encoding="utf-8-sig"))
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir()
    with httpx.Client(
        transport=httpx.HTTPTransport(local_address="0.0.0.0"),  # noqa: S104 - IPv4 egress
        timeout=120,
    ) as client:
        for name, digest in lock["files"].items():
            cached = cache / name
            if not cached.exists() or sha256(cached.read_bytes()).hexdigest() != digest:
                response = client.get(lock["base_url"] + name)
                response.raise_for_status()
                if sha256(response.content).hexdigest() != digest:
                    raise ValueError(f"Runtime hash mismatch: {name}")
                cached.write_bytes(response.content)
            shutil.copyfile(cached, output / name)


def build(output: Path, cache: Path) -> None:
    """Keep generated assets outside source and record their exact deployable inventory."""
    output = output.resolve()
    if output.exists() or output.is_relative_to(ROOT):
        raise ValueError("Choose a fresh output directory outside the repository")
    output.mkdir(parents=True)
    bun = shutil.which("bun")
    if not bun:
        raise ValueError("Bun is required to build the existing workbench")
    subprocess.run(  # noqa: S603 - explicit build argv
        [bun, "build", "./index.html", "--minify", f"--outdir={output}"],
        cwd=ROOT / "apps/web",
        check=True,
        timeout=120,
    )
    index = output / "index.html"
    index.write_text(
        index.read_text(encoding="utf-8-sig").replace("<html", '<html data-runtime="browser"', 1),
        encoding="utf-8",
    )
    index.write_text(
        index.read_text(encoding="utf-8").replace(
            "</head>", '<link rel="icon" href="data:,"></head>'
        ),
        encoding="utf-8",
    )
    bundle = {
        f"creditlens/{name}.py": (ROOT / f"src/creditlens/{name}.py").read_text(
            encoding="utf-8-sig"
        )
        for name in MODULES
    }
    bundle["bridge.py"] = (BROWSER / "bridge.py").read_text(encoding="utf-8-sig")
    bundle["fixture.json"] = json.dumps(public_fixture())
    (output / "python-bundle.json").write_text(json.dumps({"files": bundle}), encoding="utf-8")
    shutil.copyfile(BROWSER / "worker.js", output / "worker.js")
    shutil.copyfile(BROWSER / "README.md", output / "README.md")
    runtime_files(output / "runtime", cache)
    files = {
        p.relative_to(output).as_posix(): sha256(p.read_bytes()).hexdigest()
        for p in sorted(output.rglob("*"))
        if p.is_file()
    }
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()  # noqa: S603,S607 - fixed read-only command
    (output / "deployment-manifest.json").write_text(
        json.dumps(
            {
                "mode": "browser-python-lexical",
                "source_revision": revision,
                "source_dirty": bool(
                    subprocess.check_output(  # noqa: S603,S607 - fixed read-only command
                        ["git", "status", "--porcelain"],  # noqa: S607 - fixed git argv
                        cwd=ROOT,
                        text=True,
                    ).strip()
                ),
                "files": files,
                "runtime": json.loads(
                    (BROWSER / "runtime-lock.json").read_text(encoding="utf-8-sig")
                ),
                "public_synthetic_only": True,
                "server_required": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "files": len(files),
                "bytes": sum(p.stat().st_size for p in output.rglob("*") if p.is_file()),
            }
        )
    )


def main() -> None:
    """Staging does not call HF APIs, request hardware or create billable resources."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-cache", type=Path, required=True)
    args = parser.parse_args()
    build(args.output, args.runtime_cache)


if __name__ == "__main__":
    main()
