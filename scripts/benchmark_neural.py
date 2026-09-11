"""Measure composed offline model providers and fixture outcomes against unchanged page qrels."""

import argparse
import json
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from typing import Any

from creditlens.domain import Chunk, QueryRequest
from creditlens.errors import ServiceError
from creditlens.evaluation import (
    GoldCase,
    audit_chunks,
    page_key,
    read_inputs,
    score_ranking,
    summarize_outcomes,
    summarize_ranking,
)
from creditlens.evaluation_outcomes import packet_checks
from creditlens.lab_evidence import experiment_manifest, finish_manifest, save_checkpoint
from creditlens.retrieval import EvidenceCatalog
from creditlens.retrieval_cache import CanonicalProvider
from creditlens.runtime import open_search
from creditlens.settings import Settings
from creditlens.storage import GrantStore, grants, open_database
from creditlens.workflow import QueryWorkflow


def run_case(case: GoldCase, provider: CanonicalProvider, store: GrantStore) -> dict[str, Any]:
    """Use real SQL fixture grants; ranking metrics never count financial metadata lookups."""
    principal = case.principal()
    with store.engine.begin() as connection:
        connection.execute(grants.delete())
        connection.execute(grants.insert().values(**principal.model_dump(), enabled=True))
    query = QueryRequest(
        question=case.question, borrower_id=case.borrower_id, effective_at=case.effective_at
    )
    ranking: tuple[Chunk, ...] = ()
    denied = False
    started = perf_counter()
    try:
        result = provider.search(query, principal)
        provider.verify(result)
        ranking = result.chunks
    except ServiceError as error:
        if error.status != 403 or case.expected_behavior != "deny":
            raise
        denied = True
    duration = (perf_counter() - started) * 1000
    violations = audit_chunks(ranking, case)
    if violations:
        raise RuntimeError("Unauthorized composed ranking")
    score = score_ranking(
        [page_key(chunk) for chunk in ranking],
        {page_key(q): q.relevance for q in case.relevant_pages},
    )
    row: dict[str, Any] = {
        "case_id": case.case_id,
        "category": case.category,
        "score": asdict(score),
        "ranked_chunk_ids": [chunk.chunk_id for chunk in ranking],
        "ranked_pages": [page_key(chunk) for chunk in ranking],
        "ranking_latency_ms": duration,
        "denied_before_ranking": denied,
        "ranking_violations": violations,
    }
    workflow = QueryWorkflow(provider.catalog, store, provider)
    if denied:
        try:
            workflow.query(query, principal)
        except ServiceError as error:
            if error.status != 403:
                raise
            row["outcome"] = {
                "fixture_pass": True,
                "context_violations": [],
                "path_pass": True,
                "error": None,
            }
        else:
            raise RuntimeError("Expected workflow denial was bypassed")
    else:
        packet = workflow.query(query, principal)
        row["outcome"] = packet_checks(packet, case, store, retrieval_stage="retrieval.provider")
        row["packet"] = packet.model_dump(mode="json")
    return row


def execute(args: argparse.Namespace, manifest: dict[str, Any]) -> None:
    """Checkpoint each full result; any failed model or scope check invalidates execution."""
    cases, pages = read_inputs(args.gold, args.pages)
    config = Settings(
        database_url="sqlite:///:memory:",
        retrieval_mode="hybrid",
        local_model_directory=str(args.models),
    )
    engine = open_database(config.database_url)
    rows: list[dict[str, Any]] = []
    manifest.update(
        mode="local-hybrid-reranked-provider",
        query_cache="none",
        vector_cache="bounded 4096 entries, cold initially; shared across authorized requests",
        timing_scope=(
            "provider ranking with SQL grants; workflow runs separately after each ranking "
            "and reuses document vectors; no HTTP latency claim"
        ),
        packages={name: version(name) for name in ("sentence-transformers", "torch", "numpy")},
    )
    started = perf_counter()
    try:
        store = GrantStore(engine)
        with open_search(config, EvidenceCatalog(pages), store) as provider:
            if provider is None:
                raise RuntimeError("Configured hybrid provider is absent")
            manifest["startup_seconds"] = perf_counter() - started
            save_checkpoint(args.output, manifest)
            with (args.output / "records.jsonl").open("w", encoding="utf-8") as stream:
                for case in cases:
                    row = run_case(case, provider, store)
                    rows.append(row)
                    stream.write(json.dumps(row) + "\n")
                    stream.flush()
                    summary = {
                        "ranking": summarize_ranking(rows),
                        "outcomes": summarize_outcomes(rows),
                    }
                    save_checkpoint(args.output, manifest, summary=summary)
                    if len(rows) % 20 == 0:
                        print(json.dumps({"completed_cases": len(rows)}), flush=True)
    finally:
        engine.dispose()


def main() -> None:
    """Refuse to overwrite evidence and preserve failure state and exact source provenance."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=Path("evals/gold_cases.jsonl"))
    parser.add_argument("--pages", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[1]
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
