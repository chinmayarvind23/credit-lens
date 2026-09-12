"""Compare four actual local HNSW configurations on the frozen permission-aware workload."""

import argparse
import json
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import httpx

from creditlens.evaluation import (
    audit_chunks,
    page_key,
    read_inputs,
    score_ranking,
    summarize_ranking,
)
from creditlens.lab_evidence import experiment_manifest, finish_manifest, save_checkpoint
from creditlens.retrieval import EvidenceCatalog, chunk_page
from creditlens.retrieval_lab import DenseLab
from infra.weaviate.lab import create, populate, search, tune
from scripts.benchmark_faiss import snapshot


def queries(cases: Any, catalog: EvidenceCatalog, lab: DenseLab) -> list[dict[str, Any]]:
    """Freeze authorized scopes and warm embeddings once, outside all HTTP timing windows."""
    result = []
    for case in cases:
        candidates, _ = snapshot(catalog, case)
        if audit_chunks(candidates, case):
            raise RuntimeError("Unauthorized canonical scope")
        result.append(
            {
                "case": case,
                "allowed": [c.chunk_id for c in candidates],
                "vector": lab.encode_query(case.question)[0].tolist() if candidates else [],
                "exact": [c.chunk_id for c in lab.rank(case.question, candidates, limit=10)],
            }
        )
    return result


def evaluate(client: httpx.Client, name: str, workload: Any, chunks: Any, output: Path) -> Any:
    """Retain raw IDs and independently scored page relevance, including every denied case."""
    rows = []
    with output.open("w", encoding="utf-8") as stream:
        for item in workload:
            details: dict[str, Any] = {"raw_chunk_ids": [], "duplicate_count": 0}
            started = perf_counter()
            found = search(client, name, item["vector"], item["allowed"], details)
            elapsed = (perf_counter() - started) * 1000
            exact = set(item["exact"])
            case = item["case"]
            score = score_ranking(
                [page_key(chunks[k]) for k in found],
                {page_key(q): q.relevance for q in case.relevant_pages},
            )
            row = {
                **details,
                "case_id": case.case_id,
                "category": case.category,
                "ranked_chunk_ids": found,
                "exact_chunk_ids": item["exact"],
                "candidate_count": len(item["allowed"]),
                "agreement": len(set(found) & exact) / len(exact) if exact else None,
                "denied_before_ranking": not item["allowed"],
                "ranking_latency_ms": elapsed,
                "score": asdict(score),
            }
            rows.append(row)
            stream.write(json.dumps(row) + "\n")
            stream.flush()
    return summarize_ranking(rows)


def experiment(args: argparse.Namespace, manifest: dict[str, Any]) -> None:
    """Index each graph once, sweep ef, and remove only collections created successfully here."""
    cases, pages = read_inputs(args.gold, args.pages)
    chunks = tuple(c for p in pages for c in chunk_page(p))
    lab = DenseLab(chunks, args.models)
    workload = queries(cases, EvidenceCatalog(pages), lab)
    manifest["embedding_index_seconds"] = lab.indexing_seconds
    by_id = {c.chunk_id: c for c in chunks}
    summaries = {}
    with httpx.Client(base_url="http://127.0.0.1:18081", timeout=60, trust_env=False) as client:
        response = client.get("/v1/meta")
        response.raise_for_status()
        manifest["server"] = response.json()
        for connections in (8, 16):
            name = "CreditlensLab" + uuid4().hex
            create(client, name, connections)
            try:
                started = perf_counter()
                populate(client, name, list(by_id), lab.vectors)
                indexed = perf_counter() - started
                for ef in (16, 64):
                    key = f"m{connections}-ef{ef}"
                    schema = tune(client, name, ef)
                    (args.output / f"{key}-schema.json").write_text(
                        json.dumps(schema, indent=2), encoding="utf-8"
                    )
                    summary = evaluate(client, name, workload, by_id, args.output / f"{key}.jsonl")
                    summaries[key] = {"indexing_seconds": indexed, **summary}
                    save_checkpoint(args.output, manifest, summary=summaries)
                    print(key + " completed", flush=True)
            finally:
                response = client.delete(f"/v1/schema/{name}")
                response.raise_for_status()


def main() -> None:
    """Preserve failed runs and pin auxiliary adapter bytes as well as source and input hashes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=Path("evals/gold_cases.jsonl"))
    parser.add_argument("--pages", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[2]
    adapter = Path(__file__).with_name("lab.py")
    manifest = experiment_manifest(repo, args.gold, args.pages, Path(__file__))
    manifest.update(
        mode="weaviate-synthetic-hnsw-v1", adapter_sha256=sha256(adapter.read_bytes()).hexdigest()
    )
    save_checkpoint(args.output, manifest)
    try:
        experiment(args, manifest)
        manifest["execution_status"] = "completed"
    except BaseException as error:
        manifest.update(execution_status="failed", error_type=type(error).__name__)
        raise
    finally:
        stable = finish_manifest(manifest, repo, args.gold, args.pages, Path(__file__))
        if not stable or sha256(adapter.read_bytes()).hexdigest() != manifest["adapter_sha256"]:
            manifest["execution_status"] = "invalid_provenance"
        save_checkpoint(args.output, manifest)
    if manifest["execution_status"] != "completed":
        raise RuntimeError("Weaviate run did not complete with stable provenance")


if __name__ == "__main__":
    main()
