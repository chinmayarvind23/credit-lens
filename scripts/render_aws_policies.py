"""Render reviewable AWS policy documents locally; this command never calls AWS."""

import argparse
import json
import re
from pathlib import Path


def render_policies(
    account: str, output: Path, distribution_id: str | None = None
) -> tuple[Path, ...]:
    """Keep templates account-neutral and require a new evidence directory before rendering."""
    if re.fullmatch(r"[0-9]{12}", account) is None:
        raise ValueError("Expected a 12-digit verified account ID")
    if distribution_id is not None and re.fullmatch(r"[A-Z0-9]{10,32}", distribution_id) is None:
        raise ValueError("Expected an existing reviewed CloudFront distribution ID")
    output.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parents[1] / "infra/aws/iam"
    written = []
    for path in sorted(source.glob("*.json")):
        policy = json.loads(path.read_text(encoding="utf-8").replace("ACCOUNT_ID", account))
        if distribution_id is None:
            policy["Statement"] = [
                statement
                for statement in policy["Statement"]
                if "DISTRIBUTION_ID" not in json.dumps(statement)
            ]
        else:
            policy = json.loads(json.dumps(policy).replace("DISTRIBUTION_ID", distribution_id))
        compact = json.dumps(policy, separators=(",", ":"))
        if path.name.startswith("deployer-") and len(compact) > 6144:
            raise ValueError("Managed policy exceeds the IAM document size limit")
        target = output / path.name
        target.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
        written.append(target)
    return tuple(written)


def main() -> None:
    """Accept only local rendering arguments, with no attach/create/apply operation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--distribution-id")
    args = parser.parse_args()
    print(
        json.dumps(
            {
                "rendered_files": len(
                    render_policies(args.account, args.output, args.distribution_id)
                ),
                "cloudfront_authorized": args.distribution_id is not None,
                "aws_calls": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
