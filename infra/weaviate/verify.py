"""Recompute authorization, overlap and page relevance from retained server neighbor IDs."""

import argparse
import json
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from statistics import mean

from creditlens.evaluation import page_key, percentile, read_inputs, score_ranking
from creditlens.retrieval import EvidenceCatalog, chunk_page
from scripts.benchmark_faiss import snapshot


def verify(output: Path, gold: Path, pages_path: Path) -> dict:
    """Reject missing cases, changed inputs, wrong settings or any out-of-scope raw result."""
    manifest = json.loads((output / "manifest.json").read_text())
    if manifest["execution_status"] != "completed" or not manifest["provenance_stable"]:
        raise ValueError("Run is incomplete or provenance changed")
    for key, path in (("gold_sha256", gold), ("pages_sha256", pages_path)):
        if sha256(path.read_bytes()).hexdigest() != manifest[key]:
            raise ValueError("Input hash differs")
    cases, pages = read_inputs(gold, pages_path)
    catalog = EvidenceCatalog(pages)
    chunks = {c.chunk_id: c for p in pages for c in chunk_page(p)}
    results = {}
    for connections, ef in ((8, 16), (8, 64), (16, 16), (16, 64)):
        key = f"m{connections}-ef{ef}"
        schema = json.loads((output / f"{key}-schema.json").read_text())
        config = schema["vectorIndexConfig"]
        if (config["maxConnections"], config["ef"], config["flatSearchCutoff"]) != (
            connections,
            ef,
            0,
        ):
            raise ValueError("Wrong graph configuration")
        rows = [json.loads(line) for line in (output / f"{key}.jsonl").read_text().splitlines()]
        if [r["case_id"] for r in rows] != [c.case_id for c in cases]:
            raise ValueError("Case coverage or order differs")
        for row, case in zip(rows, cases, strict=True):
            allowed, _ = snapshot(catalog, case)
            ids = {c.chunk_id for c in allowed}
            raw, exact = row["raw_chunk_ids"], set(row["exact_chunk_ids"])
            ranked = list(dict.fromkeys(raw))
            expected = score_ranking(
                [page_key(chunks[k]) for k in ranked],
                {page_key(q): q.relevance for q in case.relevant_pages},
            )
            agreement = len(set(ranked) & exact) / len(exact) if exact else None
            if (
                not set(raw).issubset(ids)
                or not exact.issubset(ids)
                or row["ranked_chunk_ids"] != ranked
                or row["score"] != asdict(expected)
                or row["agreement"] != agreement
                or row["candidate_count"] != len(ids)
                or row["duplicate_count"] != len(raw) - len(ranked)
            ):
                raise ValueError("Retained result failed independent recomputation")
        eligible = [r for r in rows if r["score"]["eligible"]]
        searched = [r for r in rows if r["candidate_count"]]
        results[key] = {
            "cases": len(rows),
            "searched": len(searched),
            "eligible": len(eligible),
            "mean_ann_agreement": mean(r["agreement"] for r in searched),
            "mean_recall_at_10": mean(r["score"]["recall"] for r in eligible),
            "mean_ndcg_at_10": mean(r["score"]["ndcg"] for r in eligible),
            "p95_http_search_ms": percentile([r["ranking_latency_ms"] for r in searched], 0.95),
            "duplicate_ids": sum(r["duplicate_count"] for r in rows),
            "records_sha256": sha256((output / f"{key}.jsonl").read_bytes()).hexdigest(),
        }
    return results


def main() -> None:
    """Write a distinct verification artifact rather than modifying the original measurements."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gold", type=Path, default=Path("evals/gold_cases.jsonl"))
    parser.add_argument("--pages", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.output, args.gold, args.pages)
    (args.output / "verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
