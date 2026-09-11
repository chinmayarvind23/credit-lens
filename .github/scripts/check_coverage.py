"""Enforce reviewed line-coverage thresholds without silently excluding new source modules."""

import argparse
import json
from pathlib import Path
from typing import Any


def line_counts(report: dict[str, Any]) -> dict[str, tuple[int, int]]:
    """Validate coverage.py counts and normalize Windows paths for consistent gates."""
    result = {}
    for name, data in report["files"].items():
        normalized = name.replace("\\", "/")
        if "src/creditlens/" not in normalized:
            continue
        filename = normalized.split("src/creditlens/", 1)[1]
        covered = data["summary"]["covered_lines"]
        total = data["summary"]["num_statements"]
        if type(covered) is not int or type(total) is not int or not 0 <= covered <= total:
            raise ValueError(f"Invalid coverage counts for {filename}")
        if filename in result:
            raise ValueError(f"Duplicate coverage record for {filename}")
        result[filename] = (covered, total)
    return result


def validate_scope(policy: dict[str, Any], source_dir: Path) -> tuple[set[str], set[str]]:
    """Every source module needs a reviewed inclusion or exclusion before the gate can pass."""
    core = set(policy["core_files"])
    critical = set(policy["critical_files"])
    excluded = policy["excluded_files"]
    inventory = {path.relative_to(source_dir).as_posix() for path in source_dir.rglob("*.py")}
    if not core or not critical <= core or core & set(excluded):
        raise ValueError("Invalid coverage policy scope")
    if core | set(excluded) != inventory:
        raise ValueError("Source inventory changed; review coverage inclusions and exclusions")
    if not all(isinstance(reason, str) and reason.strip() for reason in excluded.values()):
        raise ValueError("Every coverage exclusion requires a reason")
    return core, critical


def minimum(policy: dict[str, Any], name: str) -> float:
    """Reject absent, boolean, or out-of-range thresholds rather than weakening a broken policy."""
    value = policy[name]
    if type(value) not in (int, float) or not 0 <= value <= 100:
        raise ValueError(f"Invalid threshold: {name}")
    return float(value)


def check_coverage(
    report: dict[str, Any], policy: dict[str, Any], source_dir: Path
) -> dict[str, Any]:
    """Gate the weighted core total and each critical module using unrounded statement counts."""
    core, critical = validate_scope(policy, source_dir)
    counts = line_counts(report)
    if not core <= counts.keys():
        raise ValueError("Coverage report is missing a required core module")
    threshold = minimum(policy, "core_minimum_percent")
    critical_threshold = minimum(policy, "critical_minimum_percent")
    covered = sum(counts[name][0] for name in core)
    total = sum(counts[name][1] for name in core)
    if total == 0:
        raise ValueError("The deterministic core has no measured statements")
    failures = []
    if covered * 100 < total * threshold:
        failures.append(f"Deterministic core below {threshold:g}%")
    critical_results = {}
    for name in sorted(critical):
        hit, statements = counts[name]
        if statements == 0 or hit * 100 < statements * critical_threshold:
            failures.append(f"{name} below {critical_threshold:g}%")
        critical_results[name] = {
            "covered_lines": hit,
            "statements": statements,
            "percent": hit / statements * 100 if statements else None,
        }
    return {
        "status": "fail" if failures else "pass",
        "failures": failures,
        "policy_version": policy["policy_version"],
        "measurement": policy["measurement"],
        "core_minimum_percent": threshold,
        "critical_minimum_percent": critical_threshold,
        "core": {"covered_lines": covered, "statements": total, "percent": covered / total * 100},
        "critical": critical_results,
        "core_files": sorted(core),
        "excluded_files": policy["excluded_files"],
    }


def main() -> None:
    """Print and persist the gate result so CI failure has inspectable numeric evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=Path(".github/coverage_policy.json"))
    parser.add_argument("--source-dir", type=Path, default=Path("src/creditlens"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = check_coverage(
        json.loads(args.report.read_text(encoding="utf-8")),
        json.loads(args.policy.read_text(encoding="utf-8")),
        args.source_dir,
    )
    output = json.dumps(result, indent=2) + "\n"
    args.output.write_text(output, encoding="utf-8")
    print(output)
    raise SystemExit(1 if result["failures"] else 0)


if __name__ == "__main__":
    main()
