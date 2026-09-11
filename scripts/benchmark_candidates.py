"""Compare candidate budgets using shared offline pair scores, without latency extrapolation."""

import argparse
import json
import math
import os
import sys
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from typing import Any

from creditlens.domain import Chunk
from creditlens.errors import ServiceError
from creditlens.evaluation import GoldCase, audit_chunks, page_key, read_inputs, score_ranking
from creditlens.lab_evidence import experiment_manifest, finish_manifest, save_checkpoint
from creditlens.neural_search import LocalNeuralRanker, input_budget
from creditlens.retrieval import EvidenceCatalog, lexical_rank, reciprocal_rank_fusion

BUDGETS = tuple(
    (branch, rerank)
    for branch in (20, 40, 100)
    for rerank in (10, 20, 40, 80, 100)
    if rerank <= branch
)


def rank_scores(pool: tuple[Chunk, ...], scores: dict[str, float]) -> tuple[Chunk, ...]:
    """Reject missing/invalid scores; candidates outside the prefix cannot enter the result."""
    if len({c.chunk_id for c in pool}) != len(pool):
        raise ValueError("Duplicate candidate identity")
    if any(c.chunk_id not in scores or not math.isfinite(scores[c.chunk_id]) for c in pool):
        raise ValueError("Candidate scores must be present and finite")
    return tuple(sorted(pool, key=lambda c: (-scores[c.chunk_id], c.chunk_id))[:10])


def allowed_chunks(catalog: EvidenceCatalog, case: GoldCase) -> tuple[tuple[Chunk, ...], int]:
    """Independently audit the pre-ranking boundary and retain expected early denials."""
    try:
        chunks, revision = catalog.snapshot(case.principal(), case.borrower_id, case.effective_at)
    except ServiceError as error:
        if error.status != 403 or case.expected_behavior != "deny":
            raise
        return (), 0
    if audit_chunks(chunks, case):
        raise ValueError("Unauthorized candidates before model scoring")
    return chunks, revision


def measure_case(
    case: GoldCase, catalog: EvidenceCatalog, model: LocalNeuralRanker
) -> dict[str, Any]:
    """Score each authorized pair once; branch and rerank budgets share exactly those scores."""
    chunks, revision = allowed_chunks(catalog, case)
    started = perf_counter()
    dense = model.rank(case.question, chunks, 100)
    lexical = lexical_rank(case.question, chunks, 100)
    pools = {
        f"branches{branch}-rerank{rerank}": reciprocal_rank_fusion(
            (lexical[:branch], dense[:branch]), limit=rerank, k=60
        )
        for branch, rerank in BUDGETS
    }
    union = tuple({c.chunk_id: c for pool in pools.values() for c in pool}.values())
    input_budget(case.question, union, 100, 200)
    scoring = perf_counter()
    raw = (
        model.reranker.predict(
            [(case.question, chunk.text) for chunk in union],
            batch_size=32,
            show_progress_bar=False,
        )
        if union
        else []
    )
    if len(raw) != len(union):
        raise ValueError("Model score count differs from candidates")
    scores = {c.chunk_id: float(score) for c, score in zip(union, raw, strict=True)}
    scoring_ms = (perf_counter() - scoring) * 1000
    if revision:
        catalog.verify_revision(revision)
    qrels = {page_key(q): q.relevance for q in case.relevant_pages}
    variants = {}
    for name, pool in pools.items():
        ranking = rank_scores(pool, scores)
        if audit_chunks(ranking, case):
            raise ValueError("Unauthorized result after scoring")
        variants[name] = {
            "score": asdict(score_ranking([page_key(c) for c in ranking], qrels)),
            "pool_recall": score_ranking([page_key(c) for c in pool], qrels, k=100).recall,
            "pool_size": len(pool),
            "ranked_chunk_ids": [c.chunk_id for c in ranking],
            "ranked_pages": [page_key(c) for c in ranking],
            "context_utf8_bytes": sum(len(c.text.encode()) for c in ranking),
        }
    return {
        "case_id": case.case_id,
        "category": case.category,
        "denied": revision == 0,
        "scoped_chunks": len(chunks),
        "dense_ids": [c.chunk_id for c in dense],
        "lexical_ids": [c.chunk_id for c in lexical],
        "scored_pairs": len(union),
        "pair_scoring_ms": scoring_ms,
        "experiment_ms": (perf_counter() - started) * 1000,
        "scores": scores,
        "variants": variants,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep no-qrel exclusions explicit and avoid attributing shared latency to variants."""
    results = {}
    for branch, rerank in BUDGETS:
        name = f"branches{branch}-rerank{rerank}"
        eligible = [r["variants"][name] for r in rows if r["variants"][name]["score"]["eligible"]]
        count = len(eligible)
        results[name] = {
            "scheduled_cases": len(rows),
            "eligible_cases": count,
            "excluded_cases": len(rows) - count,
            "macro_recall_at_10": sum(r["score"]["recall"] for r in eligible) / count
            if count
            else None,
            "ndcg_at_10": sum(r["score"]["ndcg"] for r in eligible) / count if count else None,
            "macro_pool_recall": sum(r["pool_recall"] for r in eligible) / count if count else None,
            "relevant_hits": sum(r["score"]["retrieved_relevant"] for r in eligible),
        }
    return results


def no_network(event: str, _args: tuple[Any, ...]) -> None:
    """Offline model experiments must not silently download weights or invoke remote services."""
    if event == "socket.connect":
        raise PermissionError("Candidate experiment forbids network connections")


def execute(args: argparse.Namespace, manifest: dict[str, Any]) -> None:
    """Retain raw pair scores and completed variants even when a later case fails."""
    cases, pages = read_inputs(args.gold, args.pages)
    catalog = EvidenceCatalog(pages)
    model = LocalNeuralRanker(args.models)
    rows = []
    manifest.update(
        mode="offline-shared-candidate-scores",
        candidate_budgets=BUDGETS,
        model_provenance=model.revision,
        cache="bounded document vectors; no query cache",
        network="socket connections denied",
        latency_scope="shared experiment only",
    )
    try:
        with (args.output / "records.jsonl").open("w", encoding="utf-8") as stream:
            for case in cases:
                row = measure_case(case, catalog, model)
                rows.append(row)
                stream.write(json.dumps(row) + "\n")
                stream.flush()
                save_checkpoint(args.output, manifest, summary=summarize(rows))
                if len(rows) % 40 == 0:
                    print(json.dumps({"completed_cases": len(rows)}), flush=True)
    finally:
        model.close()


def main() -> None:
    """Freeze source/input provenance and require a fresh private output directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("gold", "pages", "models", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    if args.output.resolve().is_relative_to(repo):
        raise ValueError("Experiment output must stay outside the repository")
    args.output.mkdir(parents=True, exist_ok=False)
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
    sys.addaudithook(no_network)
    manifest = experiment_manifest(repo, args.gold, args.pages, Path(__file__))
    save_checkpoint(args.output, manifest)
    try:
        execute(args, manifest)
        manifest["execution_status"] = "completed"
    except Exception as error:
        manifest.update(execution_status="failed", error_type=type(error).__name__)
        raise
    finally:
        if not finish_manifest(manifest, repo, args.gold, args.pages, Path(__file__)):
            manifest["execution_status"] = "invalid_provenance"
        save_checkpoint(args.output, manifest)


if __name__ == "__main__":
    main()
