"""Compare page-bounded chunk strategies with frozen qrels and one identical BM25 scorer."""

import argparse
import json
from collections.abc import Callable
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from typing import Any

from creditlens.domain import Chunk, Page
from creditlens.errors import ServiceError
from creditlens.evaluation import (
    GoldCase,
    audit_chunks,
    page_key,
    read_inputs,
    score_ranking,
    summarize_ranking,
)
from creditlens.lab_evidence import experiment_manifest, finish_manifest, save_checkpoint
from creditlens.llama_chunking import SemanticChunker, sentence_chunks, token_chunks
from creditlens.retrieval import EvidenceCatalog, chunk_page, lexical_rank
from creditlens.retrieval_lab import EMBED_MODEL, EMBED_REVISION


def evaluate_case(
    case: GoldCase, catalog: EvidenceCatalog, chunks: tuple[Chunk, ...]
) -> dict[str, Any]:
    """Use canonical allowed pages before scoring and audit derived chunks independently."""
    try:
        canonical, revision = catalog.snapshot(
            case.principal(), case.borrower_id, case.effective_at
        )
    except ServiceError as error:
        if error.status != 403 or case.expected_behavior != "deny":
            raise
        canonical, revision = (), 0
    identities = {(c.tenant_id, *page_key(c)) for c in canonical}
    candidates = tuple(c for c in chunks if (c.tenant_id, *page_key(c)) in identities)
    candidate_violations = audit_chunks(candidates, case)
    if candidate_violations:
        raise RuntimeError("Chunk strategy violated canonical permission scope")
    started = perf_counter()
    ranking = lexical_rank(case.question, candidates, limit=100)
    elapsed = (perf_counter() - started) * 1000
    if revision:
        catalog.verify_revision(revision)
    score = score_ranking(
        [page_key(c) for c in ranking], {page_key(q): q.relevance for q in case.relevant_pages}
    )
    return {
        "case_id": case.case_id,
        "category": case.category,
        "score": asdict(score),
        "ranked_chunk_ids": [c.chunk_id for c in ranking],
        "ranking_violations": audit_chunks(ranking, case),
        "candidate_violations": candidate_violations,
        "denied_before_ranking": revision == 0,
        "ranking_latency_ms": elapsed,
    }


def run_strategy(
    name: str,
    parser: Callable[[Page], tuple[Chunk, ...]],
    pages: tuple[Page, ...],
    cases: tuple[GoldCase, ...],
    output: Path,
) -> dict[str, Any]:
    """Persist source spans and every ranking; time chunk construction separately from ranking."""
    started = perf_counter()
    chunks: list[Chunk] = []
    with (output / f"{name}-chunks.jsonl").open("w", encoding="utf-8") as stream:
        for number, page in enumerate(pages, start=1):
            page_chunks = parser(page)
            chunks.extend(page_chunks)
            for chunk in page_chunks:
                stream.write(chunk.model_dump_json() + "\n")
            if number % 500 == 0:
                print(f"{name}: chunked {number}/{len(pages)} pages", flush=True)
    preprocessing = perf_counter() - started
    catalog = EvidenceCatalog(pages)
    rows = []
    with (output / f"{name}.jsonl").open("w", encoding="utf-8") as stream:
        for case in cases:
            row = evaluate_case(case, catalog, tuple(chunks))
            stream.write(json.dumps(row) + "\n")
            stream.flush()
            rows.append(row)
            if row["ranking_violations"]:
                raise RuntimeError("Unauthorized chunk ranking")
    summary = summarize_ranking(rows)
    summary.update(
        chunk_count=len(chunks),
        physical_pages=len(pages),
        mean_characters=sum(len(c.text) for c in chunks) / len(chunks),
        maximum_characters=max(len(c.text) for c in chunks),
        preprocessing_seconds=preprocessing,
        candidate_violations=sum(len(r["candidate_violations"]) for r in rows),
        ranking_violations=sum(len(r["ranking_violations"]) for r in rows),
    )
    return summary


def execute(args: argparse.Namespace, manifest: dict[str, Any]) -> None:
    """Keep fixed/paragraph controls, equal token budgets, and one explicit semantic threshold."""
    cases, pages = read_inputs(args.gold, args.pages)
    strategies: dict[str, Callable[[Page], tuple[Chunk, ...]]] = {
        "fixed-1200": fixed_chunks,
        "paragraph-1200": chunk_page,
        "token-256": token_chunks,
        "sentence-256": sentence_chunks,
    }
    results = {}
    for name, parser in strategies.items():
        results[name] = run_strategy(name, parser, pages, cases, args.output)
        save_checkpoint(args.output, manifest, summary=results)
    started = perf_counter()
    semantic = SemanticChunker(args.models)
    manifest["semantic_model_load_seconds"] = perf_counter() - started
    results["semantic-95"] = run_strategy("semantic-95", semantic.chunks, pages, cases, args.output)
    save_checkpoint(args.output, manifest, summary=results)
    print(json.dumps(results, indent=2), flush=True)


def fixed_chunks(page: Page) -> tuple[Chunk, ...]:
    """Keep the fixed-character control independent of paragraph boundary selection."""
    return chunk_page(page, strategy="fixed")


def main() -> None:
    """Preserve failures and reject changed provenance before comparing exploratory scores."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=Path, required=True)
    parser.add_argument("--gold", type=Path, default=Path("evals/gold_cases.jsonl"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = experiment_manifest(Path.cwd(), args.gold, args.pages, Path(__file__))
    manifest.update(mode="local-chunking-ablation", cache="disabled")
    manifest.update(
        embedding_model=EMBED_MODEL,
        embedding_revision=EMBED_REVISION,
        embedding_role="semantic chunk boundaries only",
        torch_threads=4,
        semantic_percentile=95,
        token_budget=256,
        token_overlap=0,
        tokenizer="tiktoken cl100k_base (LlamaIndex default)",
        preprocessing_scope="chunk construction plus JSON serialization and file writes",
        dependencies={
            name: version(name)
            for name in (
                "llama-index-core",
                "llama-index-embeddings-huggingface",
                "sentence-transformers",
                "torch",
                "tiktoken",
            )
        },
    )
    manifest["ranking"]["chunker"] = "five named page-bounded strategies"
    manifest["ranking"]["latency_scope"] = "BM25 ranking only; excludes scope, metrics and HTTP"
    save_checkpoint(args.output, manifest)
    try:
        execute(args, manifest)
        manifest["execution_status"] = "completed"
    except BaseException as error:
        manifest.update(execution_status="failed", error_type=type(error).__name__)
        raise
    finally:
        stable = finish_manifest(manifest, Path.cwd(), args.gold, args.pages, Path(__file__))
        if not stable:
            manifest["execution_status"] = "invalid_provenance"
        save_checkpoint(args.output, manifest)
    if not stable:
        raise RuntimeError("Experiment source or inputs changed during execution")


if __name__ == "__main__":
    main()
