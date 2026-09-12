"""Export reviewed evaluation artifacts as scoped Prometheus gauges without inference."""

import argparse
import json
from hashlib import sha256
from pathlib import Path

from prometheus_client import CollectorRegistry, Gauge, generate_latest


def checked(path: Path, digest: str) -> dict:
    """Require the operator-reviewed byte hash before interpreting an evaluation artifact."""
    data = path.read_bytes()
    if sha256(data).hexdigest() != digest:
        raise ValueError("Evaluation artifact hash changed")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("Evaluation report must be an object")
    return result


def count(value: object) -> int:
    """Reject booleans, negative counts and silent floating-point coercion."""
    if type(value) is not int or value < 0:
        raise ValueError("Invalid evaluation count")
    return value


def journal_tokens(journals: list, model: str) -> dict[str, int]:
    """Verify bound completed model calls before summing measured token usage."""
    tokens = {"prompt": 0, "completion": 0}
    for path, expected in journals:
        raw = path.read_bytes()
        if sha256(raw).hexdigest() != expected:
            raise ValueError("Raw evaluation journal changed")
        lines = raw.decode("utf-8").splitlines()
        if len(lines) != 1:
            raise ValueError("Expected exactly one bound call per prompt group")
        response = json.loads(lines[0])["response"]
        if (
            response.get("done") is not True
            or response.get("done_reason") != "stop"
            or response.get("model") != model
        ):
            raise ValueError("Incomplete or wrong-model token record")
        tokens["prompt"] += count(response["prompt_eval_count"])
        tokens["completion"] += count(response["eval_count"])
    return tokens


def packet_counts(data: dict) -> tuple[int, int]:
    """Check binary strict verdict coverage against the reviewed aggregate."""
    total = count(data["graded_packets"])
    scores = [row["score"] for row in data["results"]]
    if any(type(score) not in (int, float) or score not in (0, 1) for score in scores):
        raise ValueError("Strict packet verdicts must be binary")
    supported = sum(scores)
    if supported != data["raw_passes"]:
        raise ValueError("Packet verdict total differs")
    if not total or supported > total or len(data["results"]) != total:
        raise ValueError("Invalid whole-packet reconciliation")
    return total, int(supported)


def export(report: Path, digest: str, output: Path) -> dict:
    """Separate field support from packet judgments and derive tokens only from bound raw calls."""
    data = checked(report, digest)
    registry = CollectorRegistry()

    def gauge(name: str, description: str, value: float) -> None:
        """Only fixed metric names are emitted; case IDs, prompts and evidence stay private."""
        Gauge("creditlens_eval_" + name, description, registry=registry).set(value)

    if data.get("status") == "complete" and data.get("whole_packet_scored") is False:
        total, supported = count(data["graded_units"]), count(data["supported_units"])
        if not total or supported > total or count(data["failed_groups"]) != 0:
            raise ValueError("Incomplete or invalid field reconciliation")
        gauge(
            "field_support_ratio",
            "Custom RAGAS cited-field support; not packet accuracy",
            supported / total,
        )
        gauge(
            "field_occurrences", "Graded cited-field occurrences, including repeated fields", total
        )
        completed = count(data["completed_groups"])
        journals = [
            (Path(p), h)
            for p, h in data["input_hashes"].items()
            if Path(p).name.startswith("calls-") and Path(p).suffix == ".jsonl"
        ]
        if len(journals) != completed:
            raise ValueError("Missing raw call journals")
        tokens = journal_tokens(journals, data["model"])
        metric = Gauge(
            "creditlens_eval_tokens",
            "Actual local evaluation tokens, not serving usage",
            ["direction"],
            registry=registry,
        )
        for direction, value in tokens.items():
            metric.labels(direction).set(value)
        gauge("judgments", "Actual local model judgments", completed)
        scope = "custom_ragas_cited_fields"
    elif data.get("status") == "reconciled" and "raw_packet_pass_rate" in data:
        total, supported = packet_counts(data)
        gauge(
            "packet_pass_ratio",
            "Raw whole-packet GEval diagnostic, not human accuracy",
            supported / total,
        )
        gauge("packets_graded", "Whole packets with reconciled model verdicts", total)
        if type(data["controls_passed"]) is not bool:
            raise ValueError("Invalid control status")
        gauge(
            "controls_passed",
            "Synthetic judge-control check; not human calibration",
            int(data["controls_passed"]),
        )
        scope = "whole_packet_geval"
    else:
        raise ValueError("Only complete reconciled evaluation reports are supported")
    if type(data.get("human_calibrated")) is not bool:
        raise ValueError("Missing human calibration status")
    gauge(
        "human_calibrated",
        "Whether independent human calibration is established",
        int(data["human_calibrated"]),
    )
    gauge(
        "cost_known",
        "Whether monetary evaluation cost is established; local tokens do not imply a dollar cost",
        0,
    )
    output.mkdir(parents=True, exist_ok=False)
    (output / "metrics.prom").write_bytes(generate_latest(registry))
    manifest = {
        "report_sha256": digest,
        "scope": scope,
        "cost_known": False,
        "metrics_sha256": sha256((output / "metrics.prom").read_bytes()).hexdigest(),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    """Require explicit reviewed report identity and a fresh private output directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.report, args.sha256, args.output)))


if __name__ == "__main__":
    main()
