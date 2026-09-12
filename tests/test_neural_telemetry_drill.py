"""Failure lifecycle checks run without loading models or mutating production source files."""

import json
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import Mock

import pytest

from creditlens.errors import ServiceError
from scripts import check_neural_telemetry as drill


@pytest.fixture
def root() -> Iterator[Path]:
    """Keep every simulated artifact and vanished source in an owned temporary directory."""
    with TemporaryDirectory(prefix="creditlens-neural-drill-tests-") as directory:
        yield Path(directory)


def fake_telemetry(path: str) -> Mock:
    """Create only the empty local trace artifact expected after a pre-inference failure."""
    Path(path).write_text("", encoding="utf-8")
    return Mock()


def test_load_failure_has_initial_checkpoint_and_final_report(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed model constructor preserves bounded failure metadata and closes telemetry."""
    output = root / "result"
    telemetry = Mock()

    def initialize(path: str) -> Mock:
        """Check the checkpoint exists before even the optional telemetry resources are opened."""
        initial = json.loads((output / "report.json").read_text())
        assert initial["status"] == "running" and initial["source_hashes"]
        Path(path).write_text("", encoding="utf-8")
        return telemetry

    monkeypatch.setattr(drill, "Telemetry", initialize)
    monkeypatch.setattr(
        drill,
        "LocalNeuralRanker",
        Mock(side_effect=ServiceError("model_unavailable", "PRIVATE-LOAD-DETAILS")),
    )
    with pytest.raises(ServiceError, match="model_unavailable"):
        drill.run(root / "unused-models", output)
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "failed" and report["error_type"] == "ServiceError"
    assert report["source_stable"] and report["privacy_passed"]
    assert "PRIVATE-LOAD-DETAILS" not in json.dumps(report)
    telemetry.close.assert_called_once()


def test_model_close_failure_does_not_mask_inference_error(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both failures are retained while the original inference exception reaches the operator."""
    output = root / "result"
    telemetry = fake_telemetry(str(root / "placeholder.jsonl"))
    model = Mock()
    model.rank.side_effect = ValueError("PRIVATE-INFERENCE-DETAILS")
    model.close.side_effect = OSError("PRIVATE-CLEANUP-DETAILS")

    def initialize(path: str) -> Mock:
        """Make the requested trace exist without constructing any real exporter."""
        Path(path).write_text("", encoding="utf-8")
        return telemetry

    monkeypatch.setattr(drill, "Telemetry", initialize)
    monkeypatch.setattr(drill, "LocalNeuralRanker", Mock(return_value=model))
    with pytest.raises(ValueError, match="PRIVATE-INFERENCE-DETAILS"):
        drill.run(root / "unused-models", output)
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "failed" and report["error_type"] == "ValueError"
    assert report["cleanup_errors"] == [{"component": "model", "error_type": "OSError"}]
    assert "PRIVATE-" not in json.dumps(report)
    telemetry.close.assert_called_once()


@pytest.mark.parametrize("failure", ["source_deleted", "trace_deleted", "trace_private", "close"])
def test_final_inspection_errors_persist_failed_status(root: Path, failure: str) -> None:
    """Provenance, privacy or exporter failure cannot leave a passing report."""
    source = root / "source.py"
    source.write_text("fixture", encoding="utf-8")
    hashes = {str(source): sha256(source.read_bytes()).hexdigest()}
    trace = root / "traces.jsonl"
    trace.write_text("", encoding="utf-8")
    telemetry = Mock()
    if failure == "source_deleted":
        source.unlink()
    elif failure == "trace_deleted":
        trace.unlink()
    elif failure == "trace_private":
        trace.write_text("PRIVATE-FAULT-QUESTION", encoding="utf-8")
    else:
        telemetry.close.side_effect = OSError("PRIVATE-CLOSE")
    report: dict[str, Any] = {"status": "passed"}
    drill.finish(root, report, hashes, None, telemetry)
    saved = json.loads((root / "report.json").read_text())
    assert saved["status"] == "failed"
    if failure == "source_deleted":
        assert not saved["source_stable"]
        assert saved["provenance_error_type"] == "FileNotFoundError"
    if failure == "trace_deleted":
        assert not saved["privacy_passed"]
        assert saved["privacy_error_type"] == "FileNotFoundError"


def test_final_write_error_preserves_original_failure(root: Path) -> None:
    """A lost writable output directory cannot replace an already-recorded failure category."""
    (root / "report.json").mkdir()
    report: dict[str, Any] = {"status": "failed", "error_type": "ValueError"}
    drill.finish(root, report, {}, None, None)
    assert report["error_type"] == "ValueError"
    with pytest.raises(OSError):
        drill.finish(root, {"status": "passed"}, {}, None, None)
