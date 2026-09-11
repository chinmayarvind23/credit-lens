"""An interrupted HTTP experiment must retain failures and its final provenance audit."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import pytest

from scripts import benchmark_cache_http as benchmark


def redis_probe() -> Mock:
    """Only the version read is stubbed; benchmark failure tests must never need a server."""
    probe = Mock()
    probe.client.info.return_value = {"redis_version": "test-only"}
    return Mock(return_value=probe)


@pytest.mark.parametrize("deleted", [False, True])
def test_failed_run_preserves_final_provenance(
    monkeypatch: pytest.MonkeyPatch, deleted: bool
) -> None:
    """Request failure stays primary even when a runner or source disappears during cleanup."""
    monkeypatch.setenv("CREDITLENS_TEST_REDIS_URL", "redis://localhost:6379")
    monkeypatch.setattr(benchmark, "RedisBytes", redis_probe())
    monkeypatch.setattr(benchmark, "run_block", Mock(side_effect=RuntimeError("request failed")))
    initial = {"source": "initial"}
    finish = FileNotFoundError("removed runner") if deleted else initial
    monkeypatch.setattr(benchmark, "provenance", Mock(side_effect=[initial, finish]))
    with TemporaryDirectory() as directory:
        output = Path(directory) / "evidence"
        args = argparse.Namespace(
            output=output, repo=Path(__file__).resolve().parents[1], blocks=1, cycles=1
        )
        with pytest.raises(RuntimeError, match="request failed"):
            benchmark.execute(args)
        manifest = json.loads((output / "manifest.json").read_text())
        assert manifest["execution_status"] == "failed"
        assert manifest["failure_type"] == "RuntimeError"
        assert manifest["provenance_stable"] is (not deleted)
        if deleted:
            assert manifest["provenance_error_type"] == "FileNotFoundError"
        assert manifest["finished_at_utc"]


def test_completed_work_cannot_promote_changed_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful HTTP loop cannot override an end-of-run code mismatch."""
    monkeypatch.setenv("CREDITLENS_TEST_REDIS_URL", "redis://localhost:6379")
    monkeypatch.setattr(benchmark, "RedisBytes", redis_probe())
    rows = [
        {"mode": mode, "warmup": False, "http_round_trip_ms": 1, "packet": {"cache_hit": hit}}
        for mode, hit in (("uncached", False), ("redis", True))
    ]
    monkeypatch.setattr(benchmark, "run_block", Mock(side_effect=[[rows[0]], [rows[1]]]))
    monkeypatch.setattr(benchmark, "provenance", Mock(side_effect=[{"code": 1}, {"code": 2}]))
    with TemporaryDirectory() as directory:
        output = Path(directory) / "evidence"
        args = argparse.Namespace(
            output=output, repo=Path(__file__).resolve().parents[1], blocks=1, cycles=1
        )
        with pytest.raises(RuntimeError, match="changed"):
            benchmark.execute(args)
        manifest = json.loads((output / "manifest.json").read_text())
        assert manifest["execution_status"] == "failed" and not manifest["provenance_stable"]


def test_claimed_repo_must_match_imported_source() -> None:
    """The --repo argument cannot attribute measurements to an unrelated clean checkout."""
    with TemporaryDirectory() as directory:
        args = argparse.Namespace(repo=Path(directory), output=Path(directory) / "evidence")
        with pytest.raises(ValueError, match="actually imported"):
            benchmark.execute(args)
        assert not args.output.exists()
