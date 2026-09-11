"""Run the frozen local lexical control and enforce recorded security/regression policy."""

import argparse
import json
from hashlib import sha256
from pathlib import Path

from creditlens.evaluation import evaluate, gate_results


def main() -> None:
    """Require explicit evidence output and keep first-run baseline establishment visible."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=Path("evals/gold_cases.jsonl"))
    parser.add_argument("--pages", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--gates", type=Path, default=Path("evals/gates.json"))
    parser.add_argument("--outcomes", action="store_true")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    result = evaluate(args.gold, args.pages, args.output, repo, args.outcomes)
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    policy = json.loads(args.gates.read_text(encoding="utf-8"))
    failures = gate_results(result, baseline, policy)
    manifest = json.loads((args.output / "manifest.json").read_text())
    if manifest["source_changed_during_run"]:
        failures.append("source_changed_during_run")
    gate = {
        "status": "fail" if failures else "pass",
        "baseline_comparison": "evaluated" if baseline else "initial_baseline_recorded",
        "failures": failures,
        "semantic_quality": "unmeasured",
        "policy": policy,
        "policy_sha256": sha256(args.gates.read_bytes()).hexdigest(),
        "baseline_summary_sha256": sha256(args.baseline.read_bytes()).hexdigest()
        if args.baseline
        else None,
    }
    (args.output / "gate.json").write_text(json.dumps(gate, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "ranking": result["ranking"],
                "execution": result["execution"],
                "outcomes": result["outcomes"],
                "gate": gate,
            },
            indent=2,
        )
    )
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
