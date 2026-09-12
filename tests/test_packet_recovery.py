"""Recovery contracts preserve frozen judgments and permit only an empty evidence list change."""

import ast
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from scripts.recover_packet_evaluation import digest, read_whole_cases, recover, verify_retained


@pytest.fixture
def recovery_dir() -> Iterator[Path]:
    """Own disposable fixtures without pytest's Windows junction cleanup path."""
    with TemporaryDirectory(prefix="creditlens-packet-recovery-") as directory:
        yield Path(directory)


def frozen_case() -> dict:
    """Represent one complete refusal without fabricating a source for a source-less question."""
    return {
        "id": "frozen-001",
        "input": "What evidence supports this?",
        "actual_output": '{"abstained": true}',
        "expected_output": "Assess the complete packet",
        "retrieval_context": [],
    }


def write_cases(path: Path, rows: list[dict]) -> bytes:
    """Retain exact input bytes so recovery can be checked independently of parsed values."""
    data = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
    path.write_bytes(data)
    return data


def original_reader():
    """Load only the existing parser AST, avoiding SDK imports, model calls and global hooks."""
    source = Path(__file__).resolve().parents[1] / "infra/evaluation/run_ragas.py"
    parsed = ast.parse(source.read_text())
    function = next(
        n for n in parsed.body if isinstance(n, ast.FunctionDef) and n.name == "read_cases"
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {"json": json, "Path": Path, "Any": object}
    exec(compile(module, str(source), "exec"), namespace)  # noqa: S102
    return namespace["read_cases"]


def test_empty_context_preserves_case_and_nonempty_matches_original(recovery_dir: Path) -> None:
    """Only[] gains admission; accepted nonempty inputs retain identical parsed and byte values."""
    path = recovery_dir / "cases.jsonl"
    row = frozen_case()
    raw = write_cases(path, [row])
    assert read_whole_cases(path) == (raw, [row])
    with pytest.raises(ValueError, match="nonempty source"):
        original_reader()(path)
    row["retrieval_context"] = ["exact source\nwith preserved whitespace"]
    write_cases(path, [row])
    assert read_whole_cases(path) == original_reader()(path)


@pytest.mark.parametrize("context", [None, "", "source", {}, [""], ["  "], [None], [42]])
def test_other_invalid_contexts_remain_rejected(recovery_dir: Path, context: object) -> None:
    """Missing, mistyped and blank sources must not become the newly permitted empty list."""
    row = frozen_case()
    row["retrieval_context"] = context
    path = recovery_dir / "cases.jsonl"
    write_cases(path, [row])
    with pytest.raises(ValueError):
        read_whole_cases(path)
    with pytest.raises(ValueError):
        original_reader()(path)


@pytest.mark.parametrize("field", ["id", "input", "actual_output", "expected_output"])
def test_required_text_is_not_relaxed(recovery_dir: Path, field: str) -> None:
    """Expected guidance remains mandatory just as the original advice runner requires."""
    row = frozen_case()
    row[field] = " "
    path = recovery_dir / "cases.jsonl"
    write_cases(path, [row])
    with pytest.raises(ValueError):
        read_whole_cases(path)


def test_input_bounds_and_duplicate_ids(recovery_dir: Path) -> None:
    """Neither the batch budget nor input identity may silently change during recovery."""
    path = recovery_dir / "cases.jsonl"
    for rows in [
        [],
        [frozen_case(), frozen_case()],
        [dict(frozen_case(), id=str(i)) for i in range(25)],
    ]:
        write_cases(path, rows)
        with pytest.raises(ValueError):
            read_whole_cases(path)
    path.write_bytes(b" " * 8_000_001)
    with pytest.raises(ValueError, match="eight megabytes"):
        read_whole_cases(path)


@pytest.mark.parametrize("field,value", [("expected_range", [1, 0]), ("expected_statements", [])])
def test_screening_contract_is_preserved(recovery_dir: Path, field: str, value: object) -> None:
    """Recovery cannot relax retained screening ranges or expected extraction units."""
    row = dict(frozen_case(), retrieval_context=["source"])
    row[field] = value
    path = recovery_dir / "cases.jsonl"
    write_cases(path, [row])
    for reader in (original_reader(), read_whole_cases):
        with pytest.raises(ValueError):
            reader(path)


def recovery_fixture(recovery_dir: Path) -> tuple[Path, dict]:
    """Keep one retained failure verdict and one unstarted batch in a synthetic manifest."""
    original = recovery_dir / "original"
    original.mkdir()
    complete = original / "controls"
    complete.mkdir()
    retained = complete / "summary.json"
    retained.write_text(
        json.dumps({"model": "recorded-model", "digest": "recorded-digest", "score": 0})
    )
    calls = complete / "judge-calls.jsonl"
    calls.write_text('{"frozen": "raw failure"}\n')
    cases = recovery_dir / "cases.jsonl"
    write_cases(cases, [frozen_case()])
    summary = {
        "status": "failed",
        "input_stable": True,
        "metric": "whole-packet-lending-v2",
        "input_hashes": {str(cases): digest(cases)},
        "batches": [
            {
                "run": str(complete),
                "cases": str(cases),
                "status": "completed",
                "artifact_hashes": {p.name: digest(p) for p in complete.iterdir()},
            },
            {
                "run": str(original / "batch-001"),
                "cases": str(cases),
                "status": "failed",
                "artifact_hashes": {},
                "error_type": "ValueError",
            },
        ],
    }
    (original / "summary.json").write_text(json.dumps(summary))
    return original, summary


def test_partial_batches_and_tampered_completed_evidence_rejected(recovery_dir: Path) -> None:
    """A partial attempt cannot be regraded, and even a retained losing verdict is immutable."""
    original, summary = recovery_fixture(recovery_dir)
    Path(summary["batches"][1]["run"]).mkdir()
    with pytest.raises(ValueError, match="partially"):
        verify_retained(summary)
    summary["batches"] = summary["batches"][:1]
    (original / "controls/summary.json").write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        verify_retained(summary)


def test_recovery_reuses_completed_verdicts_and_records_adapter(
    recovery_dir: Path, monkeypatch
) -> None:
    """Only the unstarted batch executes; original verdicts and source bytes survive."""
    original, before = recovery_fixture(recovery_dir)
    original_bytes = (original / "summary.json").read_bytes()
    called = []

    def run(args) -> None:
        """Stand in only for batch execution to inspect unchanged input and model arguments."""
        called.append(args)
        assert read_whole_cases(args.cases)[1] == [frozen_case()]
        args.output.mkdir()
        (args.output / "summary.json").write_text('{"status": "completed"}')

    fake = SimpleNamespace(run=run)
    monkeypatch.setitem(sys.modules, "run_advice_eval", fake)
    monkeypatch.setattr(sys, "path", list(sys.path))
    output = recovery_dir / "derived"
    recover(original, output)
    result = json.loads((output / "summary.json").read_text())
    assert len(called) == 1
    assert called[0].model == "recorded-model" and called[0].digest == "recorded-digest"
    assert result["batches"][0] == before["batches"][0]
    assert (original / "summary.json").read_bytes() == original_bytes
    assert result["recovery"]["original_sha256"] == digest(original / "summary.json")
    assert (
        result["input_hashes"][result["recovery"]["adapter"]]
        == result["recovery"]["adapter_sha256"]
    )
    assert result["status"] == "completed" and result["input_stable"]
    with pytest.raises(ValueError, match="fresh"):
        recover(original, output)


def test_frozen_input_change_prevents_any_recovery_output(recovery_dir: Path) -> None:
    """A changed frozen population fails before any new output or inference is permitted."""
    original, summary = recovery_fixture(recovery_dir)
    Path(summary["batches"][1]["cases"]).write_text("changed")
    output = recovery_dir / "derived"
    with pytest.raises(ValueError, match="changed"):
        recover(original, output)
    assert not output.exists()
