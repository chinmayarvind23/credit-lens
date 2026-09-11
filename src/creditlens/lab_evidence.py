"""Persist exploratory model runs even when a provider, model or security check fails."""

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from creditlens.evaluation import run_manifest


def experiment_manifest(repo: Path, gold: Path, pages: Path, script: Path) -> dict[str, Any]:
    """Capture code and inputs before model loading; exploratory results never imply promotion."""
    manifest = run_manifest(repo, gold, pages)
    manifest.update(
        benchmark_script_sha256=sha256(script.read_bytes()).hexdigest(),
        status="exploratory_not_promoted",
        execution_status="running",
    )
    return manifest


def save_checkpoint(output: Path, manifest: dict[str, Any], **payloads: Any) -> None:
    """Replace individual complete JSON files atomically so interrupted writes remain readable."""
    for name, payload in {"manifest": manifest, **payloads}.items():
        destination = output / f"{name}.json"
        temporary = output / f"{name}.json.tmp"
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)


def finish_manifest(
    manifest: dict[str, Any], repo: Path, gold: Path, pages: Path, script: Path
) -> bool:
    """Reject comparisons whose source or input bytes changed while inference was running."""
    try:
        current = experiment_manifest(repo, gold, pages, script)
    except OSError as error:
        manifest.update(
            finished_at_utc=datetime.now(UTC).isoformat(),
            provenance_stable=False,
            provenance_error_type=type(error).__name__,
        )
        return False
    keys = ("source_hashes", "gold_sha256", "pages_sha256", "benchmark_script_sha256")
    changed = [key for key in keys if manifest[key] != current[key]]
    manifest.update(
        finished_at_utc=datetime.now(UTC).isoformat(),
        changed_provenance_fields=changed,
        provenance_stable=not changed,
    )
    return not changed
