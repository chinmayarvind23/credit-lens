"""Coverage policy tests reject threshold failures and hidden changes to measured scope."""

# Assertions are deliberate test checks, matching the repository's test policy.
# ruff: noqa: S101

import importlib.util
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def gate() -> ModuleType:
    """Load the standalone CI checker without adding CI code to the application package."""
    source = Path(__file__).resolve().parents[1] / "scripts" / "check_coverage.py"
    spec = importlib.util.spec_from_file_location("check_coverage", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source() -> Iterator[Path]:
    """Create a bounded source inventory without pytest's Windows symlink helper."""
    with tempfile.TemporaryDirectory(prefix="creditlens-coverage-test-") as directory:
        root = Path(directory)
        for name in ("auth.py", "workflow.py", "corpus.py"):
            (root / name).write_text("pass\n", encoding="utf-8")
        yield root


def policy() -> dict[str, Any]:
    """A small policy makes aggregate and per-module behavior independently testable."""
    return {
        "policy_version": "test",
        "measurement": "lines",
        "core_minimum_percent": 85,
        "critical_minimum_percent": 95,
        "core_files": ["auth.py", "workflow.py"],
        "critical_files": ["auth.py"],
        "excluded_files": {"corpus.py": "Synthetic generator"},
    }


def report(auth: int = 95, workflow: int = 85) -> dict[str, Any]:
    """Use 100 statements per module so boundary assertions stay explicit."""
    return {
        "files": {
            "src\\creditlens\\auth.py": {"summary": {"covered_lines": auth, "num_statements": 100}},
            "src/creditlens/workflow.py": {
                "summary": {"covered_lines": workflow, "num_statements": 100}
            },
        }
    }


def test_threshold_boundary_passes(gate: ModuleType, source: Path) -> None:
    """The stated inclusive thresholds pass without rounding up an insufficient result."""
    result = gate.check_coverage(report(95, 75), policy(), source)
    assert result["status"] == "pass"
    assert result["core"]["percent"] == 85


def test_critical_failure_survives_high_total(gate: ModuleType, source: Path) -> None:
    """Strong ordinary coverage cannot hide weak coverage in an authorization module."""
    result = gate.check_coverage(report(94, 100), policy(), source)
    assert result["status"] == "fail"
    assert result["failures"] == ["auth.py below 95%"]


def test_core_failure_is_enforced(gate: ModuleType, source: Path) -> None:
    """A passing security module cannot hide insufficient aggregate core coverage."""
    result = gate.check_coverage(report(95, 74), policy(), source)
    assert result["status"] == "fail"
    assert "Deterministic core below 85%" in result["failures"]


def test_new_module_requires_scope_review(gate: ModuleType, source: Path) -> None:
    """New implementation files cannot fall outside coverage without an explicit policy change."""
    (source / "new_module.py").write_text("pass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="inventory changed"):
        gate.check_coverage(report(), policy(), source)


def test_missing_module_is_not_treated_as_covered(gate: ModuleType, source: Path) -> None:
    """Partial coverage reports fail instead of reporting a percentage from the surviving files."""
    data = report()
    del data["files"]["src\\creditlens\\auth.py"]
    with pytest.raises(ValueError, match="missing a required"):
        gate.check_coverage(data, policy(), source)


def test_invalid_counts_fail(gate: ModuleType, source: Path) -> None:
    """Impossible coverage values cannot manufacture a passing gate."""
    with pytest.raises(ValueError, match="Invalid coverage counts"):
        gate.check_coverage(report(101), policy(), source)
