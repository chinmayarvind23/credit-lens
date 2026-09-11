"""An interrupted routing experiment must retain the declared intervention and failure state."""

import importlib.util
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from typing import Any

import pytest


def benchmark_module() -> ModuleType:
    """Load the offline runner without exposing experiment toggles in the application package."""
    path = Path(__file__).resolve().parents[1] / "scripts/benchmark_intent.py"
    spec = importlib.util.spec_from_file_location("benchmark_intent", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_failure_checkpoint_precedes_evaluator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve variant and hash before failure so aborted runs retain their evidence."""
    module = benchmark_module()
    with TemporaryDirectory(prefix="creditlens-intent-ablation-") as directory:
        root = Path(directory)
        output = root / "run"
        checkpoint = root / "run.ablation.json"

        def fail(*args: Any, **kwargs: Any) -> None:
            """Assert evidence already exists when the dependency raises an injected failure."""
            metadata = json.loads(checkpoint.read_text())
            assert metadata["execution_status"] == "running"
            assert metadata["variant"] == "topic-only"
            assert len(metadata["script_sha256_at_start"]) == 64
            raise RuntimeError("injected failure")

        monkeypatch.setattr(module.runpy, "run_path", fail)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "benchmark_intent",
                "--variant",
                "topic-only",
                "--output",
                str(output),
                "--baseline",
                str(root / "baseline.json"),
                "--outcomes",
            ],
        )
        with pytest.raises(RuntimeError, match="injected failure"):
            module.main()
        metadata = json.loads(checkpoint.read_text())
        assert metadata["execution_status"] == "failed"
        assert metadata["error_type"] == "RuntimeError"
        assert metadata["script_changed_during_run"] is False


@pytest.mark.parametrize("mutation", ["change", "delete"])
def test_changed_runner_cannot_pass(monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    """A passing evaluator does not promote an experiment whose own runner changed mid-run."""
    module = benchmark_module()
    with TemporaryDirectory(prefix="creditlens-intent-ablation-") as directory:
        root = Path(directory)
        script = root / "runner.py"
        script.write_text("before")
        monkeypatch.setattr(module, "__file__", str(script))
        output = root / "run"
        baseline = root / "summary.json"
        baseline.write_text("{}")
        (root / "cases.jsonl").write_text('{"case_id":"example","ranked_chunk_ids":[]}\n')

        def complete(*args: Any, **kwargs: Any) -> None:
            """Emit successful evaluator evidence and alter only the temporary runner."""
            output.mkdir()
            (output / "summary.json").write_text('{"gold_sha256":"gold","pages_sha256":"pages"}')
            (output / "cases.jsonl").write_text((root / "cases.jsonl").read_text())
            if mutation == "delete":
                script.unlink()
            else:
                script.write_text("after")
            raise SystemExit(0)

        monkeypatch.setattr(module.runpy, "run_path", complete)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "benchmark_intent",
                "--variant",
                "combined",
                "--output",
                str(output),
                "--baseline",
                str(baseline),
                "--outcomes",
            ],
        )
        with pytest.raises(SystemExit) as failure:
            module.main()
        assert failure.value.code == 1
        metadata = json.loads((output / "ablation.json").read_text())
        assert metadata["script_changed_during_run"] is True
        assert metadata["ranking_ids_unchanged"] is True
        assert metadata["execution_status"] == "failed"
        if mutation == "delete":
            assert metadata["provenance_error_type"] == "FileNotFoundError"
