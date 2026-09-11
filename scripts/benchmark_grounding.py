"""Measure scoped borrower-name query expansion without changing questions or serving behavior."""

import argparse
import json
import os
import sys
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any

from creditlens.evaluation import GoldCase, audit_chunks, page_key, read_inputs, score_ranking
from creditlens.lab_evidence import experiment_manifest, finish_manifest, save_checkpoint
from creditlens.neural_search import LocalNeuralRanker
from creditlens.query_grounding import grounded_query
from creditlens.retrieval import EvidenceCatalog, lexical_rank, reciprocal_rank_fusion
from creditlens.search_provider import validate_ranking
from scripts.benchmark_candidates import allowed_chunks, no_network

VARIANTS = ("baseline", "universal", "selective")


def case_row(
    case: GoldCase, baseline: dict[str, Any], catalog: EvidenceCatalog, model: LocalNeuralRanker
) -> dict[str, Any]:
    """Keep labels outside ranking and validate reused baseline evidence against current scope."""
    allowed, revision = allowed_chunks(catalog, case)
    by_id = {c.chunk_id: c for c in allowed}
    original = tuple(by_id[identity] for identity in baseline["ranked_chunk_ids"])
    validate_ranking(original, allowed, 10)
    expanded, selected, reason = grounded_query(case.question, case.borrower_id, allowed)
    started = perf_counter()
    if expanded == case.question:
        augmented = original
    else:
        dense = model.rank(expanded, allowed, 100)
        lexical = lexical_rank(expanded, allowed, 100)
        pool = reciprocal_rank_fusion((lexical, dense), limit=40, k=60)
        augmented = model.rerank(expanded, pool, 10)
    if revision:
        catalog.verify_revision(revision)
    variants = {}
    for name, ranking in (
        ("baseline", original),
        ("universal", augmented),
        ("selective", augmented if selected else original),
    ):
        if audit_chunks(ranking, case):
            raise ValueError("Unauthorized result in query grounding experiment")
        variants[name] = {
            "score": asdict(
                score_ranking(
                    [page_key(c) for c in ranking],
                    {page_key(q): q.relevance for q in case.relevant_pages},
                )
            ),
            "ranked_chunk_ids": [c.chunk_id for c in ranking],
            "ranked_pages": [page_key(c) for c in ranking],
        }
    return {
        "case_id": case.case_id,
        "category": case.category,
        "original_question": case.question,
        "expanded_question": expanded,
        "selected": selected,
        "selection_reason": reason,
        "denied": revision == 0,
        "augmented_experiment_ms": (perf_counter() - started) * 1000,
        "variants": variants,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Report the same eligible denominator and category regressions for all three variants."""
    result: dict[str, Any] = {}
    for name in VARIANTS:
        groups = {"all": rows} | {
            category: [r for r in rows if r["category"] == category]
            for category in sorted({r["category"] for r in rows})
        }
        result[name] = {}
        for category, members in groups.items():
            values = [
                r["variants"][name]["score"]
                for r in members
                if r["variants"][name]["score"]["eligible"]
            ]
            count = len(values)
            result[name][category] = {
                "scheduled": len(members),
                "eligible": count,
                "recall": sum(v["recall"] for v in values) / count if count else None,
                "ndcg": sum(v["ndcg"] for v in values) / count if count else None,
                "hits": sum(v["retrieved_relevant"] for v in values),
            }
    return result


def execute(args: argparse.Namespace, manifest: dict[str, Any]) -> None:
    """Require a completed, matching baseline and retain augmented results including regressions."""
    previous = json.loads((args.baseline / "manifest.json").read_text())
    if previous["execution_status"] != "completed" or not previous["provenance_stable"]:
        raise ValueError("Baseline must be completed with stable provenance")
    if any(previous[key] != manifest[key] for key in ("gold_sha256", "pages_sha256")):
        raise ValueError("Baseline inputs do not match")
    cases, pages = read_inputs(args.gold, args.pages)
    baseline_rows = [
        json.loads(line) for line in (args.baseline / "records.jsonl").read_text().splitlines()
    ]
    baseline = {row["case_id"]: row["variants"]["branches100-rerank40"] for row in baseline_rows}
    if len(baseline) != len(baseline_rows) or set(baseline) != {c.case_id for c in cases}:
        raise ValueError("Baseline must contain every unique case")
    catalog = EvidenceCatalog(pages)
    model = LocalNeuralRanker(args.models)
    rows = []
    manifest.update(
        mode="offline-borrower-query-grounding",
        model_provenance=model.revision,
        variants=VARIANTS,
        network="socket connections denied",
        cache="bounded document vectors; reused baseline rankings",
    )
    try:
        with (args.output / "records.jsonl").open("w", encoding="utf-8") as stream:
            for case in cases:
                row = case_row(case, baseline[case.case_id], catalog, model)
                rows.append(row)
                stream.write(json.dumps(row) + "\n")
                stream.flush()
                save_checkpoint(args.output, manifest, summary=summarize(rows))
                if len(rows) % 40 == 0:
                    print(json.dumps({"completed_cases": len(rows)}), flush=True)
    finally:
        model.close()


def main() -> None:
    """Freeze imported experiment helpers and baseline bytes as well as code and authored labels."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("gold", "pages", "models", "baseline", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    if args.output.resolve().is_relative_to(repo):
        raise ValueError("Keep experimental outputs outside the repository")
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
    sys.addaudithook(no_network)
    manifest = experiment_manifest(repo, args.gold, args.pages, Path(__file__))
    extras = [
        Path(__file__).with_name("benchmark_candidates.py"),
        args.baseline / "manifest.json",
        args.baseline / "records.jsonl",
    ]
    before = {str(p): sha256(p.read_bytes()).hexdigest() for p in extras}
    manifest["additional_input_hashes"] = before
    save_checkpoint(args.output, manifest)
    try:
        execute(args, manifest)
        manifest["execution_status"] = "completed"
    except Exception as error:
        manifest.update(execution_status="failed", error_type=type(error).__name__)
        raise
    finally:
        stable = finish_manifest(manifest, repo, args.gold, args.pages, Path(__file__))
        if before != {str(p): sha256(p.read_bytes()).hexdigest() for p in extras} or not stable:
            manifest.update(execution_status="invalid_provenance", provenance_stable=False)
        save_checkpoint(args.output, manifest)


if __name__ == "__main__":
    main()
