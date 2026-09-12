"""Reject raw judgment tampering before any whole-packet quality score is reported."""

import copy
import json

import pytest
from advice_rubric import validate_advice_score
from reconcile_full_packet import validate_call


def fixture():
    """Retain the independent raw JSON and parsed SDK representation for mutation checks."""
    output = {"score": 1, "reason": "The policy answer matches the cited threshold."}
    case = {"id": "control"}
    call = {
        "prompt": "frozen prompt",
        "response": {
            "done": True,
            "done_reason": "stop",
            "model": "qwen3:8b",
            "message": {"content": json.dumps(output)},
        },
    }
    result = {"id": "control", **output, "outputs": [{"schema": "ReasonScore", "output": output}]}
    return case, call, result


def test_raw_verdict_is_reconciled():
    """One intact binary verdict retains its case identity and reason."""
    case, call, result = fixture()
    assert (  # noqa: S101 - test assertions are the verification contract.
        validate_call(case, call, result, "frozen prompt", "qwen3:8b", validate_advice_score)[
            "score"
        ]
        == 1
    )


@pytest.mark.parametrize(
    "change", ["prompt", "id", "model", "stop", "reason", "boolean", "fraction"]
)
def test_tampered_verdict_is_rejected(change):
    """Neither altered identity nor numeric coercion can become a complete result."""
    case, call, result = copy.deepcopy(fixture())
    if change == "prompt":
        call["prompt"] = "another packet"
    elif change == "id":
        result["id"] = "another case"
    elif change == "model":
        call["response"]["model"] = "other:tag"
    elif change == "stop":
        call["response"]["done_reason"] = "length"
    elif change == "reason":
        result["reason"] = "Changed explanation"
    else:
        raw = json.loads(call["response"]["message"]["content"])
        raw["score"] = True if change == "boolean" else 0.9
        call["response"]["message"]["content"] = json.dumps(raw)
    with pytest.raises(ValueError):
        validate_call(case, call, result, "frozen prompt", "qwen3:8b", validate_advice_score)
