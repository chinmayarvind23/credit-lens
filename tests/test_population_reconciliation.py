"""Adversarial evidence-accounting tests; fixtures exercise integrity rather than judge accuracy."""

import copy
import json
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from scripts.reconcile_population import prompt_groups, reconcile_rows, validate_counters


def fixture():
    """Use two repeated occurrences and a distinct unsupported field with explicit raw verdicts."""
    units = [
        {
            "id": str(i),
            "case_id": str(i),
            "field_path": "/claim/0",
            "actual_output": "same" if i < 2 else "different",
        }
        for i in range(3)
    ]
    coverage = [{"case_id": str(i), "fields": [{"unit_ids": [str(i)]}]} for i in range(3)]
    groups = prompt_groups(units, lambda unit: unit["actual_output"])
    rows, calls = [], []
    for index, (key, group) in enumerate(groups.items()):
        output = {
            "statements": [
                {
                    "statement": group["unit"]["actual_output"],
                    "reason": "Protocol fixture",
                    "verdict": 1 - index,
                }
            ]
        }
        rows.append(
            {
                "prompt_sha256": key,
                "unit_ids": group["ids"],
                "status": "completed",
                "score": 1 - index,
                "outputs": [{"schema": "NLIStatementOutput", "output": output}],
            }
        )
        calls.append(
            [
                {
                    "prompt": group["prompt"],
                    "response": {
                        "model": "local:tag",
                        "done": True,
                        "done_reason": "stop",
                        "message": {"content": json.dumps(output)},
                    },
                }
            ]
        )
    return units, coverage, groups, rows, calls


def test_repeated_fields_preserve_weight_and_unrun_work():
    """One verified raw call may cover repeated occurrences but cannot score an unrun group."""
    units, coverage, groups, rows, calls = fixture()
    partial = reconcile_rows(units, coverage, groups, rows[:1], calls.__getitem__, "local:tag")
    assert partial["completed_groups"] == 1 and partial["graded_units"] == 2
    assert partial["unit_statuses"] == {"supported": 2, "unrun": 1}
    full = reconcile_rows(units, coverage, groups, rows, calls.__getitem__, "local:tag")
    assert full["supported_units"] == 2 and full["supported_groups"] == 1
    assert full["unit_statuses"] == {"supported": 2, "unsupported": 1}


@pytest.mark.parametrize(
    "alteration", ["alias", "prompt", "score", "raw", "model", "stop", "statement"]
)
def test_completed_claims_require_exact_raw_evidence(alteration):
    """A plausible aggregate cannot conceal altered aliases, model identity or raw content."""
    units, coverage, groups, rows, calls = fixture()
    if alteration == "alias":
        rows[0]["unit_ids"] = ["0"]
    elif alteration == "prompt":
        calls[0][0]["prompt"] = "altered"
    elif alteration == "score":
        rows[0]["score"] = 0
    elif alteration == "raw":
        calls[0][0]["response"]["message"]["content"] = '{"statements": []}'
    elif alteration == "model":
        calls[0][0]["response"]["model"] = "other:tag"
    elif alteration == "stop":
        calls[0][0]["response"]["done_reason"] = "length"
    else:
        output = copy.deepcopy(rows[0]["outputs"][0]["output"])
        output["statements"][0]["statement"] = "only part"
        rows[0]["outputs"][0]["output"] = output
        calls[0][0]["response"]["message"]["content"] = json.dumps(output)
    with pytest.raises(ValueError):
        reconcile_rows(units, coverage, groups, rows, calls.__getitem__, "local:tag")


def test_failed_judgment_and_missing_inventory_do_not_become_passes():
    """Failures are explicit and every field must exist exactly once in the source ledger."""
    units, coverage, groups, rows, calls = fixture()
    rows[1] = {
        "prompt_sha256": sha256(b"different").hexdigest(),
        "unit_ids": ["2"],
        "status": "failed",
    }
    result = reconcile_rows(units, coverage, groups, rows, calls.__getitem__, "local:tag")
    assert result["failed_groups"] == 1 and result["unit_statuses"]["judge_failed"] == 1
    with pytest.raises(ValueError):
        reconcile_rows(units, coverage[:-1], groups, rows, calls.__getitem__, "local:tag")
    with pytest.raises(ValueError):
        validate_counters(result, dict(result, graded_units=3))


@pytest.fixture
def snapshot_directory():
    """Use ordinary temporary storage without Windows-rejected pytest current-directory links."""
    with TemporaryDirectory(prefix="creditlens-reconcile-") as directory:
        yield Path(directory)


def test_live_snapshot_allows_append_but_rejects_rewriting(snapshot_directory):
    """An immutable completed prefix can be reported while later model calls finish."""
    from scripts.reconcile_population import result_prefix, stable_snapshot

    summary = snapshot_directory / "summary.json"
    results = snapshot_directory / "results.jsonl"
    before = {"model": "local:tag", "status": "running", "completed_groups": 1, "failed_groups": 0}
    summary.write_text(json.dumps(before), encoding="utf-8")
    results.write_bytes(b"first\n")
    snapshots = {summary: summary.read_bytes(), results: results.read_bytes()}
    summary.write_text(json.dumps(dict(before, completed_groups=2)), encoding="utf-8")
    results.write_bytes(b"first\nsecond\n")
    assert stable_snapshot(snapshots, summary, results, False)
    assert not stable_snapshot(snapshots, summary, results, True)
    assert result_prefix([1, 2], before, False) == [1]
    results.write_bytes(b"changed\nsecond\n")
    assert not stable_snapshot(snapshots, summary, results, False)
    with pytest.raises(ValueError):
        result_prefix([], before, False)
