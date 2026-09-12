"""Ensure monitoring cannot turn incomplete or altered evaluation records into results."""

import json
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from infra.monitoring.serve_snapshot import payload
from scripts.export_evaluation_metrics import count, export, journal_tokens


def packet_report() -> dict:
    """Keep the test identity private while exercising binary GEval aggregate checks."""
    return {
        "status": "reconciled",
        "raw_packet_pass_rate": 0.5,
        "graded_packets": 2,
        "raw_passes": 1.0,
        "results": [{"id": "private-case", "score": 1.0}, {"score": 0.0}],
        "controls_passed": True,
        "human_calibrated": False,
    }


def test_packet_export_has_scoped_values_without_payloads():
    """Real float-valued strict verdicts remain valid without exposing case identifiers."""
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        report = root / "report.json"
        report.write_text(json.dumps(packet_report()), encoding="utf-8")
        result = export(report, sha256(report.read_bytes()).hexdigest(), root / "output")
        metrics = (root / "output/metrics.prom").read_text()
        assert "creditlens_eval_packet_pass_ratio 0.5" in metrics
        assert "creditlens_eval_cost_known 0.0" in metrics
        assert "private-case" not in metrics
        assert result["scope"] == "whole_packet_geval"
        assert payload(root / "output") == (root / "output/metrics.prom").read_bytes()
        (root / "output/metrics.prom").write_bytes(b"altered")
        with pytest.raises(ValueError, match="snapshot hash changed"):
            payload(root / "output")
        with pytest.raises(ValueError, match="hash changed"):
            export(report, "0" * 64, root / "bad")


@pytest.mark.parametrize("value", [True, -1, 1.5, "2", None])
def test_invalid_counts(value):
    """Do not silently coerce malformed usage records."""
    with pytest.raises(ValueError):
        count(value)


@pytest.mark.parametrize(
    "change",
    [
        {"status": "running"},
        {"raw_passes": 2},
        {"graded_packets": 3},
        {"results": [{"score": True}, {"score": 0}]},
        {"controls_passed": "true"},
    ],
)
def test_invalid_packet_reports(change):
    """Reject incomplete coverage, altered sums and ambiguous calibration flags."""
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        report = root / "report.json"
        report.write_text(json.dumps(packet_report() | change), encoding="utf-8")
        with pytest.raises(ValueError):
            export(report, sha256(report.read_bytes()).hexdigest(), root / "output")
        assert not (root / "output").exists()


def test_token_records_require_hash_model_and_completion():
    """Raw model usage is accepted only with matching provenance and a clean stop."""
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "calls-0000.jsonl"
        response = {
            "model": "local",
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 13,
            "eval_count": 7,
        }
        path.write_text(json.dumps({"response": response}) + "\n", encoding="utf-8")
        digest = sha256(path.read_bytes()).hexdigest()
        assert journal_tokens([(path, digest)], "local") == {"prompt": 13, "completion": 7}
        with pytest.raises(ValueError, match="wrong-model"):
            journal_tokens([(path, digest)], "other")
        path.write_text(
            json.dumps({"response": response | {"done_reason": "length"}}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="changed"):
            journal_tokens([(path, digest)], "local")
        with pytest.raises(ValueError, match="Incomplete"):
            journal_tokens([(path, sha256(path.read_bytes()).hexdigest())], "local")
