"""Run declared routing ablations through the unchanged reviewed evaluation CLI."""

import argparse
import json
import runpy
import sys
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from creditlens.intent import QueryIntent, classify_intent
from creditlens.retrieval import terms

LEGACY_FINANCE = frozenset(
    {"dscr", "coverage", "financial", "underwriting", "packet", "exception", "conflict", "missing"}
)


def ablation_intent(question: str, variant: str) -> QueryIntent:
    """Isolate each new behavior without exposing experiment switches to the serving application."""
    intent = classify_intent(question)
    if variant == "routing-only":
        return replace(intent, requires_topic_support=False, topic_terms=frozenset())
    if variant == "topic-only":
        return replace(intent, financial_review=bool(set(terms(question)) & LEGACY_FINANCE))
    return intent


def ranked_ids(path: Path) -> dict[str, list[str]]:
    """Compare actual per-case ranking outputs instead of inferring invariance from aggregates."""
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return {row["case_id"]: row["ranked_chunk_ids"] for row in rows}


def main() -> None:
    """Preserve the exact intervention and script hash beside normal per-case and gate evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--variant", choices=("routing-only", "topic-only", "combined"), required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--outcomes", action="store_true", required=True)
    args, forwarded = parser.parse_known_args()
    repo = Path(__file__).resolve().parents[1]

    def classify(question: str) -> QueryIntent:
        """Keep the declared variant fixed for every scheduled question in this process."""
        return ablation_intent(question, args.variant)

    checkpoint = args.output.with_name(args.output.name + ".ablation.json")
    if args.output.exists() or checkpoint.exists():
        raise FileExistsError("Ablation output and checkpoint must be new")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "variant": args.variant,
        "development_set_exposure": True,
        "semantic_quality": "unmeasured",
        "started_at_utc": datetime.now(UTC).isoformat(),
        "execution_status": "running",
        "script_sha256_at_start": sha256(Path(__file__).read_bytes()).hexdigest(),
        "intervention": "question intent classification only; evaluator and ranking unmodified",
    }
    checkpoint.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    status = 1
    try:
        with (
            patch("creditlens.workflow.classify_intent", side_effect=classify),
            patch.object(
                sys,
                "argv",
                [
                    "evaluate.py",
                    "--output",
                    str(args.output),
                    "--baseline",
                    str(args.baseline),
                    "--outcomes",
                    *forwarded,
                ],
            ),
        ):
            try:
                runpy.run_path(str(repo / "scripts/evaluate.py"), run_name="__main__")
            except SystemExit as result:
                status = int(result.code or 0)
        summary = json.loads((args.output / "summary.json").read_text(encoding="utf-8"))
        same_rankings = ranked_ids(args.output / "cases.jsonl") == ranked_ids(
            args.baseline.parent / "cases.jsonl"
        )
        metadata.update(
            gold_sha256=summary["gold_sha256"],
            pages_sha256=summary["pages_sha256"],
            ranking_ids_unchanged=same_rankings,
        )
        status = status or (0 if same_rankings else 1)
    except BaseException as failure:
        status = 1
        metadata["error_type"] = type(failure).__name__
        raise
    finally:
        try:
            metadata["script_sha256_at_end"] = sha256(Path(__file__).read_bytes()).hexdigest()
        except OSError as failure:
            metadata["script_sha256_at_end"] = None
            metadata["provenance_error_type"] = type(failure).__name__
        changed = metadata["script_sha256_at_start"] != metadata["script_sha256_at_end"]
        metadata["script_changed_during_run"] = changed
        status = status or int(changed)
        metadata["execution_status"] = "failed" if status else "completed"
        metadata["finished_at_utc"] = datetime.now(UTC).isoformat()
        output = json.dumps(metadata, indent=2) + "\n"
        checkpoint.write_text(output, encoding="utf-8")
        if args.output.is_dir():
            (args.output / "ablation.json").write_text(output, encoding="utf-8")
    raise SystemExit(status)


if __name__ == "__main__":
    main()
