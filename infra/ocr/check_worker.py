"""Run one real trusted synthetic scan through PostgreSQL job ownership into OCR quarantine."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from sqlalchemy.engine import make_url

from creditlens.corpus import borrower_pages
from creditlens.domain import Principal
from creditlens.ingestion_jobs import IngestionInput, JobStore, initialize_jobs
from creditlens.ingestion_worker import IngestionWorker
from creditlens.source_store import LocalSourceStore
from creditlens.sql_catalog import initialize_catalog
from creditlens.storage import grants, open_database
from infra.ocr.worker import NativeOcrExtractor


def execute(args):
    """Only a disposable loopback database may receive these generated synthetic authority rows."""
    url = os.environ["CREDITLENS_TEST_POSTGRES_URL"]
    parsed = make_url(url)
    if parsed.host not in {"127.0.0.1", "localhost"} or parsed.database != "creditlens_test":
        raise ValueError("Use disposable loopback creditlens_test")
    args.output.mkdir(parents=True, exist_ok=False)
    engine = open_database(url)
    initialize_jobs(engine)
    store = JobStore(engine, "synthetic-ocr-" + uuid4().hex)
    catalog = initialize_catalog(engine, store.queue_id)
    actor = Principal(
        subject=uuid4().hex,
        tenant_id="demo-bank",
        role="admin",
        borrower_ids=("borrower-001",),
        acl_groups=("underwriting",),
        revision=1,
    )
    with engine.begin() as connection:
        connection.execute(grants.insert().values(**actor.model_dump(mode="json"), enabled=True))
    sources = LocalSourceStore(args.output / "sources")
    digest = sources.stage(actor.tenant_id, args.pdf.read_bytes())
    page = borrower_pages(1)[0].model_copy(
        update={
            "document_id": "borrower-001-ocr-financial",
            "document_kind": "financial_statement",
            "title": "Synthetic financial scan",
            "section": "financials",
        }
    )
    source = IngestionInput(source_sha256=digest, pages=(page,), parser="ocr")
    job = store.submit(source, actor, "one-scan")
    extractor = NativeOcrExtractor(
        args.python, args.models, args.renderer, args.output / "artifacts"
    )
    worker = IngestionWorker(store, sources, catalog, None, ocr_extractor=extractor)
    files = [Path(__file__).with_name(name) for name in ("worker.py", "probe.py", "completion.py")]
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    try:
        result = worker.run_one(job.job_id)
        status = store.status(job.job_id, actor)
        report = {
            "result": result.model_dump(mode="json"),
            "status": status.model_dump(mode="json"),
            "job_id": job.job_id,
            "queue_id": store.queue_id,
            "source_sha256": digest,
            "extractor_hashes": before,
        }
        if result.state != "REVIEW_REQUIRED" or status.state != "REVIEW_REQUIRED":
            raise RuntimeError("Actual OCR did not reach durable review")
        artifact = store.review_artifact(job.job_id, actor)
        if catalog.snapshot(actor, "borrower-001", page.valid_from)[0]:
            raise RuntimeError("Unreviewed OCR was published")
        report.update(
            artifact_sha256=artifact.digest(),
            unreviewed_catalog_empty=True,
            duplicate_delivery=store.delivery_disposition(job.job_id, include_ocr=True),
            source_stable=before
            == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        )
        (args.output / "review.json").write_text(
            artifact.model_dump_json(indent=2), encoding="utf-8"
        )
        (args.output / "verification.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        print(json.dumps(report), flush=True)
        if not report["source_stable"]:
            raise RuntimeError("Extractor code changed during verification")
    finally:
        engine.dispose()


def main():
    """Require explicit fixture/model paths and retain review artifacts outside the repository."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("pdf", "python", "models", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--renderer", required=True)
    execute(parser.parse_args())


if __name__ == "__main__":
    main()
