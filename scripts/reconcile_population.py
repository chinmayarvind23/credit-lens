"""Reconcile population NLI results against original fields and raw journals without inference."""

import argparse
import json
import sys
from collections import Counter
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace


def prompt_groups(units, render):
    """Group only identical actual prompts while preserving every original field identity."""
    groups = {}
    for unit in units:
        prompt = render(unit)
        key = sha256(prompt.encode()).hexdigest()
        group = groups.setdefault(key, {"prompt": prompt, "unit": unit, "ids": []})
        if group["prompt"] != prompt:
            raise ValueError("Prompt identity collision")
        group["ids"].append(unit["id"])
    return groups


def verify_verdict(row, group, calls, model):
    """Require one completed raw generation, the exact prompt and the unmodified full statement."""
    if len(calls) != 1:
        raise ValueError("A completed prompt requires exactly one raw call")
    call = calls[0]
    response = call["response"]
    if (
        call["prompt"] != group["prompt"]
        or response.get("model") != model
        or response.get("done") is not True
        or response.get("done_reason") != "stop"
    ):
        raise ValueError("Raw generation identity or completion differs")
    output = json.loads(response["message"]["content"])
    if row["outputs"] != [{"schema": "NLIStatementOutput", "output": output}]:
        raise ValueError("Result output differs from its raw model journal")
    statements = output["statements"]
    if len(statements) != 1:
        raise ValueError("Verdict must preserve the whole field exactly once")
    verdict = statements[0]
    if (
        verdict["statement"] != group["unit"]["actual_output"]
        or type(verdict["verdict"]) is not int
        or verdict["verdict"] not in (0, 1)
        or not isinstance(verdict.get("reason"), str)
        or not verdict["reason"].strip()
        or isinstance(row["score"], bool)
        or row["score"] != verdict["verdict"]
    ):
        raise ValueError("Verdict statement or score differs from the original field")
    return verdict["verdict"]


def reconcile_rows(units, coverage, groups, rows, read_calls, model):
    """Recompute field outcomes from exact aliases and retain failed or unrun work explicitly."""
    indexed = {unit["id"]: unit for unit in units}
    inventory = [
        identity
        for case in coverage
        for field in case.get("fields", [])
        for identity in field.get("unit_ids", [])
    ]
    if len(indexed) != len(units) or Counter(inventory) != Counter(indexed.keys()):
        raise ValueError("Population ledger does not cover every unique exported field")
    if len(rows) > len(groups):
        raise ValueError("Result count exceeds planned prompts")
    ledger = {identity: {"status": "unrun", "score": None} for identity in indexed}
    completed, failed, supported_groups = 0, 0, 0
    for index, ((key, group), row) in enumerate(zip(groups.items(), rows, strict=False)):
        if row["prompt_sha256"] != key or row["unit_ids"] != group["ids"]:
            raise ValueError("Result order, prompt or aliases differ from frozen inputs")
        if row["status"] == "completed":
            score = verify_verdict(row, group, read_calls(index), model)
            completed += 1
            supported_groups += score
            state = "supported" if score else "unsupported"
        elif row["status"] == "failed":
            failed += 1
            score, state = None, "judge_failed"
        else:
            raise ValueError("Unknown result status")
        for identity in group["ids"]:
            ledger[identity] = {"status": state, "score": score, "prompt_sha256": key}
    fields = [
        {
            "unit_id": identity,
            "case_id": indexed[identity]["case_id"],
            "field_path": indexed[identity]["field_path"],
            **state,
        }
        for identity, state in ledger.items()
    ]
    counts = Counter(field["status"] for field in fields)
    return {
        "completed_groups": completed,
        "failed_groups": failed,
        "supported_groups": supported_groups,
        "unit_statuses": dict(counts),
        "graded_units": counts["supported"] + counts["unsupported"],
        "supported_units": counts["supported"],
        "fields": fields,
    }


def sdk_renderer():
    """Construct the pinned RAGAS prompt with evaluation telemetry disabled and no model object."""
    # These imports initialize SDK privacy before importing its prompt classes.
    import ragas_judge  # noqa: F401
    from ragas.metrics.collections.faithfulness.util import (  # noqa: I001
        NLIStatementInput,
        NLIStatementPrompt,
        StatementGeneratorPrompt,
    )
    from ragas_profiles import configure

    metric = SimpleNamespace(
        statement_generator_prompt=StatementGeneratorPrompt(),
        nli_statement_prompt=NLIStatementPrompt(),
    )
    instructions = configure(metric, "lending-v1")

    def render(unit):
        """Use the library serializer so examples, instructions and context all enter the hash."""
        return metric.nli_statement_prompt.to_string(
            NLIStatementInput(
                context="\n".join(unit["retrieval_context"]), statements=[unit["actual_output"]]
            )
        )

    return render, instructions


def validate_counters(report, summary):
    """A running summary is a convenience only; reject counts that disagree with raw evidence."""
    for key in ("completed_groups", "failed_groups", "graded_units", "supported_units"):
        if report[key] != summary[key]:
            raise ValueError("Run summary differs from independently reconciled counts")


def block_network(event, args):
    """Verification uses local files only, including during SDK import and prompt rendering."""
    if event == "socket.connect":
        raise PermissionError("Population reconciliation does not permit network connections")


def stable_snapshot(snapshots, summary_path, results_path, terminal):
    """Allow live append-only progress, but reject any mutation of previously observed evidence."""
    progress = {
        "status",
        "completed_groups",
        "failed_groups",
        "graded_units",
        "supported_units",
        "ungraded_units",
        "provenance_stable",
        "input_stable",
    }
    for path, original in snapshots.items():
        current = path.read_bytes()
        if not terminal and path == results_path:
            if not current.startswith(original):
                return False
        elif not terminal and path == summary_path:
            before, after = json.loads(original), json.loads(current)
            if {k: v for k, v in before.items() if k not in progress} != {
                k: v for k, v in after.items() if k not in progress
            }:
                return False
        elif current != original:
            return False
    return True


def result_prefix(rows, summary, terminal):
    """A live snapshot uses the completed prefix acknowledged by its captured summary."""
    if terminal:
        return rows
    recorded = summary["completed_groups"] + summary["failed_groups"]
    if len(rows) < recorded:
        raise ValueError("Summary is ahead of its raw results")
    return rows[:recorded]


def build_report(args):
    """Accept a stable snapshot; only terminal complete runs may pass final reconciliation."""
    repo = Path(__file__).resolve().parents[1]
    if args.output.exists() or args.output.resolve().is_relative_to(repo):
        raise ValueError("Use a new report path outside the repository")
    sys.path.insert(0, str(repo / "infra/evaluation"))
    from run_population import read_population

    render, instructions = sdk_renderer()
    snapshots = {}

    def read(path):
        """Retain exact input bytes so a concurrent append cannot produce a mixed-time report."""
        data = path.read_bytes()
        snapshots[path] = data
        return data

    manifest, units, input_hash = read_population(args.population)
    coverage = json.loads(read(args.population / "coverage.json"))
    summary = json.loads(read(args.run / "summary.json"))
    rows = [
        json.loads(line)
        for line in read(args.run / "results.jsonl").decode().splitlines()
        if line.strip()
    ]
    terminal = summary["status"] in {"completed", "completed_with_errors", "interrupted"}
    observed_rows = len(rows)
    rows = result_prefix(rows, summary, terminal)
    if (
        summary["input_sha256"] != input_hash
        or summary["case_count"] != len(coverage)
        or manifest["case_count"] != len(coverage)
        or len({case["case_id"] for case in coverage}) != len(coverage)
    ):
        raise ValueError("Population identity or case count differs")
    for path, digest in summary["source_hashes"].items():
        if sha256(read(Path(path))).hexdigest() != digest:
            raise ValueError("Evaluator source differs from the recorded run")
    if instructions != summary["instruction_hashes"]:
        raise ValueError("Metric instructions differ from the recorded run")
    groups = prompt_groups(units, render)
    if len(groups) != summary["unique_prompts"] or len(units) != summary["unit_count"]:
        raise ValueError("Run population or prompt count differs")

    def calls(index):
        """Bind a completed group to its original ordered raw journal."""
        return [
            json.loads(line)
            for line in read(args.run / f"calls-{index:04d}.jsonl").decode().splitlines()
            if line.strip()
        ]

    report = reconcile_rows(units, coverage, groups, rows, calls, summary["model"])
    validate_counters(report, summary)
    complete = (
        terminal
        and summary["status"] == "completed"
        and len(rows) == len(groups)
        and report["graded_units"] == len(units)
        and summary.get("provenance_stable") is True
        and summary.get("input_stable") is True
    )
    report.update(
        status="complete" if complete else "partial",
        observed_result_rows=observed_rows,
        reconciled_result_rows=len(rows),
        case_count=len(coverage),
        cases=coverage,
        unique_prompts=len(groups),
        unit_count=len(units),
        human_calibrated=False,
        whole_packet_scored=False,
        model=summary["model"],
        digest=summary["digest"],
        run_status=summary["status"],
        controls_sha256=summary["controls_sha256"],
        field_support_rate=(report["supported_units"] / len(units) if complete else None),
        scope="Exact cited-field support; question relevance and whole packets ungraded",
        source_commit_scope="Input and evaluator hashes; no inferred production accuracy",
    )
    if read_population(args.population)[2] != input_hash or not stable_snapshot(
        snapshots, args.run / "summary.json", args.run / "results.jsonl", terminal
    ):
        raise ValueError("Retained evidence changed during reconciliation")
    report["input_hashes"] = {
        str(path): sha256(data).hexdigest() for path, data in snapshots.items()
    }
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2))
    return complete


def main():
    """Never call a model or network service while verifying persisted evaluation evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("population", "run", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    sys.addaudithook(block_network)
    complete = build_report(args)
    if args.require_complete and not complete:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
