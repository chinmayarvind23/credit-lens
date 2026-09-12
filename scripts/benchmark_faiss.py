"""Retain independently checkable scoped HNSW results without rerunning the cross-encoder."""

import argparse
import json
from pathlib import Path
from typing import Any

from creditlens.errors import ServiceError
from creditlens.evaluation import audit_chunks, read_inputs
from creditlens.lab_evidence import experiment_manifest, finish_manifest, save_checkpoint
from creditlens.retrieval import EvidenceCatalog, chunk_page
from creditlens.retrieval_lab import DenseLab


def snapshot(catalog: EvidenceCatalog, case: Any) -> Any:
    """Retain expected authorization denials without swallowing unexpected service errors."""
    try:
        return catalog.snapshot(case.principal(), case.borrower_id, case.effective_at)
    except ServiceError as error:
        if error.status != 403 or case.expected_behavior != "deny":
            raise
        return (), 0


def run(args: argparse.Namespace) -> None:
    """Keep frozen inputs and all denied cases; write each real ANN result before continuing."""
    args.output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[1]
    manifest = experiment_manifest(repo, args.gold, args.pages, Path(__file__))
    manifest.update(
        mode="scoped-faiss-auditable-v1",
        hnsw={"M": 16, "efConstruction": 80, "efSearch": 64},
        query_cache="warmed before measured ANN search",
    )
    save_checkpoint(args.output, manifest)
    try:
        cases, pages = read_inputs(args.gold, args.pages)
        catalog = EvidenceCatalog(pages)
        lab = DenseLab(tuple(c for p in pages for c in chunk_page(p)), args.models)
        manifest.update(
            embedding_index_seconds=lab.indexing_seconds, vector_bytes=int(lab.vectors.nbytes)
        )
        with (args.output / "records.jsonl").open("w", encoding="utf-8") as stream:
            for case in cases:
                candidates, revision = snapshot(catalog, case)
                if audit_chunks(candidates, case):
                    raise RuntimeError("Unauthorized candidates")
                if candidates:
                    lab.encode_query(case.question)
                result = lab.faiss_agreement(case.question, candidates)
                allowed = {c.chunk_id for c in candidates}
                if not set(result["approximate_chunk_ids"]).issubset(allowed):
                    raise RuntimeError("Unauthorized ANN result")
                if revision:
                    catalog.verify_revision(revision)
                stream.write(json.dumps({"case_id": case.case_id, **result}) + "\n")
                stream.flush()
        manifest["execution_status"] = "completed"
    except BaseException as error:
        manifest.update(execution_status="failed", error_type=type(error).__name__)
        raise
    finally:
        if not finish_manifest(manifest, repo, args.gold, args.pages, Path(__file__)):
            manifest["execution_status"] = "invalid_provenance"
        save_checkpoint(args.output, manifest)
    if manifest["execution_status"] != "completed":
        raise RuntimeError("FAISS run did not complete with stable provenance")


def main() -> None:
    """Require existing local models and explicit retained evidence output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=Path("evals/gold_cases.jsonl"))
    parser.add_argument("--pages", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
