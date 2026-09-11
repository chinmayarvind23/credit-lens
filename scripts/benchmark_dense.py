"""Compare local model rankers on frozen authored page qrels without changing the baseline."""

import argparse
import importlib
import json
import os
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx

from creditlens.errors import ServiceError
from creditlens.evaluation import (
    audit_chunks,
    page_key,
    read_inputs,
    score_ranking,
    summarize_ranking,
)
from creditlens.lab_evidence import experiment_manifest, finish_manifest, save_checkpoint
from creditlens.retrieval import EvidenceCatalog, chunk_page, lexical_rank, reciprocal_rank_fusion
from creditlens.retrieval_lab import (
    EMBED_MODEL,
    EMBED_REVISION,
    RERANK_MODEL,
    RERANK_REVISION,
    DenseLab,
)


def ipv4_client() -> httpx.Client:
    """Use a scoped IPv4 client because this host's IPv6 route resets TLS connections."""
    return httpx.Client(
        transport=httpx.HTTPTransport(local_address="0.0.0.0", retries=2),  # noqa: S104 - outbound IPv4 client, not a listening server
        follow_redirects=True,
        timeout=60,
    )


def execute(args: argparse.Namespace, manifest: dict[str, Any]) -> None:
    """Checkpoint each completed case, retaining partial rankings when later inference fails."""
    cases, pages = read_inputs(args.gold, args.pages)
    manifest["packages"] = {
        name: version(name)
        for name in ("sentence-transformers", "torch", "numpy", "faiss-cpu", "llama-index-core")
    }
    catalog = EvidenceCatalog(pages)
    lab = DenseLab(tuple(chunk for page in pages for chunk in chunk_page(page)), args.models)
    manifest.update(
        {
            "mode": "local-model-retrieval-lab",
            "embedding_model": EMBED_MODEL,
            "embedding_revision": EMBED_REVISION,
            "reranker": RERANK_MODEL,
            "reranker_revision": RERANK_REVISION,
            "indexing_seconds": lab.indexing_seconds,
            "embedding_snapshot_seconds": lab.embedding_snapshot_seconds,
            "embedding_load_seconds": lab.embedding_load_seconds,
            "embedding_dimensions": int(lab.vectors.shape[1]),
            "vector_bytes": int(lab.vectors.nbytes),
            "query_cache": "shared within experiment",
            "rerank_candidates": 40,
        }
    )
    save_checkpoint(args.output, manifest)
    records: dict[str, list[dict[str, Any]]] = {
        name: [] for name in ("dense", "hybrid", "reranked")
    }
    ann = []
    for case in cases:
        try:
            candidates, revision = catalog.snapshot(
                case.principal(), case.borrower_id, case.effective_at
            )
        except ServiceError as error:
            if error.status != 403 or case.expected_behavior != "deny":
                raise
            candidates, revision = (), 0
        candidate_violations = audit_chunks(candidates, case)
        if candidate_violations:
            save_checkpoint(
                args.output,
                manifest,
                failure={"case_id": case.case_id, "candidate_violations": candidate_violations},
            )
            raise RuntimeError("Unauthorized candidate before model scoring")
        dense_started = perf_counter()
        dense = lab.rank(case.question, candidates)
        dense_seconds = perf_counter() - dense_started
        hybrid_started = perf_counter()
        lexical = lexical_rank(case.question, candidates, limit=100)
        hybrid = reciprocal_rank_fusion((dense, lexical), limit=100)
        hybrid_seconds = dense_seconds + perf_counter() - hybrid_started
        rerank_started = perf_counter()
        reranked = lab.rerank(case.question, hybrid[:40])
        rerank_seconds = perf_counter() - rerank_started
        if revision:
            catalog.verify_revision(revision)
        ann.append({"case_id": case.case_id, **lab.faiss_agreement(case.question, candidates)})
        for name, ranking, elapsed in (
            ("dense", dense, dense_seconds),
            ("hybrid", hybrid, hybrid_seconds),
            ("reranked", reranked, hybrid_seconds + rerank_seconds),
        ):
            score = score_ranking(
                [page_key(chunk) for chunk in ranking],
                {page_key(qrel): qrel.relevance for qrel in case.relevant_pages},
            )
            row = {
                "case_id": case.case_id,
                "category": case.category,
                "score": asdict(score),
                "ranked_chunk_ids": [c.chunk_id for c in ranking],
                "ranking_violations": audit_chunks(ranking, case),
                "candidate_violations": candidate_violations,
                "denied_before_ranking": revision == 0,
                "ranking_latency_ms": elapsed * 1000,
            }
            records[name].append(row)
            with (args.output / f"{name}.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row) + "\n")
            if row["ranking_violations"]:
                raise RuntimeError("Unauthorized ranking result")
        save_checkpoint(args.output, manifest, hnsw=ann)
        if len(ann) % 20 == 0:
            print(f"Evaluated {len(ann)}/{len(cases)}", flush=True)
    summaries = {name: summarize_ranking(rows) for name, rows in records.items()}
    for name, rows in records.items():
        summaries[name]["candidate_violations"] = sum(len(r["candidate_violations"]) for r in rows)
        summaries[name]["ranking_violations"] = sum(len(r["ranking_violations"]) for r in rows)
    save_checkpoint(args.output, manifest, summary=summaries, hnsw=ann)
    print(json.dumps(summaries, indent=2))


def main() -> None:
    """Retain failure provenance without disguising incomplete runs as comparable measurements."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=Path, required=True)
    parser.add_argument("--gold", type=Path, default=Path("evals/gold_cases.jsonl"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = experiment_manifest(Path.cwd(), args.gold, args.pages, Path(__file__))
    manifest.update(
        mode="local-model-retrieval-lab",
        embedding_model=EMBED_MODEL,
        embedding_revision=EMBED_REVISION,
        reranker=RERANK_MODEL,
        reranker_revision=RERANK_REVISION,
        cache="query vectors shared within experiment",
    )
    manifest["ranking"]["latency_scope"] = (
        "Local ranking with query-vector reuse; reranker first load included. "
        "Excludes snapshot, authorization, audit and HTTP."
    )
    manifest["ranking"]["methods"] = ["numpy-cosine", "bm25-rrf", "cross-encoder-top40"]
    save_checkpoint(args.output, manifest)
    try:
        os.environ["HF_HUB_DISABLE_XET"] = "1"
        hub = importlib.import_module("huggingface_hub")
        hub.set_client_factory(ipv4_client)
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
        raise RuntimeError("Experiment code or input bytes changed during the run")


if __name__ == "__main__":
    main()
