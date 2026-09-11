"""Ablation ranking retains authorization even when a forbidden page has perfect query terms."""

import importlib.util
from hashlib import sha256
from pathlib import Path

from creditlens.corpus import build_demo_pages
from creditlens.evaluation import GoldCase
from creditlens.retrieval import EvidenceCatalog, chunk_page


def test_chunking_ablation_excludes_forbidden_page() -> None:
    """Derived chunks of a restricted page must be absent before the shared BM25 scorer runs."""
    source = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_chunking.py"
    spec = importlib.util.spec_from_file_location("benchmark_chunking", source)
    assert spec is not None and spec.loader is not None
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    gold = source.parent.parent / "evals" / "gold_cases.jsonl"
    case = GoldCase.model_validate_json(gold.read_text(encoding="utf-8").splitlines()[0])
    pages = list(build_demo_pages())
    forbidden = next(p for p in pages if p.borrower_id == "borrower-001" and p.page == 18)
    text = (case.question + " ") * 100
    poisoned = forbidden.model_copy(
        update={"text": text, "content_hash": sha256(text.encode()).hexdigest()}
    )
    pages[pages.index(forbidden)] = poisoned
    chunks = tuple(chunk for page in pages for chunk in chunk_page(page, size=200))
    denied_ids = {
        c.chunk_id for c in chunks if c.document_id == poisoned.document_id and c.page == 18
    }
    row = benchmark.evaluate_case(case, EvidenceCatalog(tuple(pages)), chunks)
    assert row["ranked_chunk_ids"]
    assert not denied_ids.intersection(row["ranked_chunk_ids"])
    assert row["candidate_violations"] == row["ranking_violations"] == []
