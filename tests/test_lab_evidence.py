"""Model-run evidence must survive interruption and detect changed experiment inputs."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from creditlens.lab_evidence import experiment_manifest, finish_manifest, save_checkpoint


def test_provenance_and_checkpoint() -> None:
    """Persist failure metadata and detect changed inputs without running model inference."""
    with TemporaryDirectory() as directory:
        output = Path(directory)
        gold, pages, script = (output / name for name in ("gold.jsonl", "pages.jsonl", "run.py"))
        for path in (gold, pages, script):
            path.write_text("initial", encoding="utf-8")
        manifest = experiment_manifest(Path.cwd(), gold, pages, script)
        assert finish_manifest(manifest, Path.cwd(), gold, pages, script)
        pages.write_text("changed", encoding="utf-8")
        assert not finish_manifest(manifest, Path.cwd(), gold, pages, script)
        assert manifest["changed_provenance_fields"] == ["pages_sha256"]
        manifest.update(execution_status="failed", error_type="RuntimeError")
        save_checkpoint(output, manifest, summary={"completed_cases": 2})
        saved = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        assert saved["status"] == "exploratory_not_promoted"
        assert saved["execution_status"] == "failed"
        assert json.loads((output / "summary.json").read_text())["completed_cases"] == 2
        assert not tuple(output.glob("*.tmp"))


def test_deleted_provenance_marks_invalid() -> None:
    """A deleted input must produce a saved invalid manifest rather than leave a run active."""
    with TemporaryDirectory() as directory:
        output = Path(directory)
        inputs = [output / name for name in ("gold", "pages", "script")]
        for path in inputs:
            path.write_text("initial", encoding="utf-8")
        manifest = experiment_manifest(Path.cwd(), *inputs)
        inputs[1].unlink()
        assert not finish_manifest(manifest, Path.cwd(), *inputs)
        assert manifest["provenance_error_type"] == "FileNotFoundError"
        manifest["execution_status"] = "invalid_provenance"
        save_checkpoint(output, manifest)
        saved = json.loads((output / "manifest.json").read_text())
        assert saved["execution_status"] == "invalid_provenance"
