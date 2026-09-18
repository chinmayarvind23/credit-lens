"""Extend the opt-in real RAG fixture with authorized ingestion and vector synchronization."""

import argparse
import json
import os
import ssl
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
import jwt
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete

from creditlens.corpus import _write_pdf, borrower_pages
from creditlens.domain import Packet
from creditlens.ingestion import text_hash
from creditlens.ingestion_jobs import IngestionInput
from creditlens.neural_search import LocalNeuralRanker
from creditlens.settings import Settings
from creditlens.source_store import LocalSourceStore
from creditlens.storage import audit_events, grants
from creditlens.weaviate_store import WeaviateStore
from scripts.index_weaviate import synchronize
from scripts.ingest_documents import execute


def exercise_governed_ingestion(
    client: TestClient, settings: Settings, key, token, subject
) -> None:
    """A real PDF reaches only its bound catalog, then becomes searchable after explicit sync."""
    store = client.app.state.store
    actor = store.resolve(subject).model_copy(update={"subject": uuid4().hex, "role": "admin"})
    with store.engine.begin() as connection:
        connection.execute(grants.insert().values(**actor.model_dump(mode="json"), enabled=True))
    claims = jwt.decode(
        token, key.public_key(), algorithms=["RS256"], options={"verify_aud": False}
    )
    admin_token = jwt.encode(claims | {"sub": actor.subject}, key, algorithm="RS256")
    user_headers = {"Authorization": "Bearer " + token}
    admin_headers = {"Authorization": "Bearer " + admin_token, "Idempotency-Key": uuid4().hex}
    resources = Path(__file__).resolve().parents[2] / "resources/credit_lens/artifacts"
    resources.mkdir(parents=True, exist_ok=True)
    try:
        with TemporaryDirectory(prefix="governed-ingestion-", dir=resources) as directory:
            source_root = Path(directory) / "sources"
            source = stage_inspection_pdf(Path(directory), source_root)
            refused = client.post(
                "/api/v1/admin/documents",
                json=source.model_dump(mode="json"),
                headers=user_headers | {"Idempotency-Key": uuid4().hex},
            )
            assert refused.status_code == 403
            admitted = client.post(
                "/api/v1/admin/documents",
                json=source.model_dump(mode="json"),
                headers=admin_headers,
            )
            assert admitted.status_code == 202, admitted.text
            job_id = admitted.json()["job_id"]
            again = client.post(
                "/api/v1/admin/documents",
                json=source.model_dump(mode="json"),
                headers=admin_headers,
            )
            assert again.status_code == 202 and again.json()["job_id"] == job_id
            args = argparse.Namespace(
                command="work-one",
                job_id=job_id,
                source_root=source_root,
                queue_endpoint=None,
                queue_url=None,
                image=os.environ["CREDITLENS_TEST_PARSER_IMAGE"],
                timeout=60,
            )
            assert json.loads(execute(args, settings))["state"] == "COMPLETED"
            status = client.get("/api/v1/admin/index-jobs/" + job_id, headers=admin_headers)
            assert status.status_code == 200 and status.json()["state"] == "COMPLETED"
            query = {
                "borrower_id": "borrower-001",
                "effective_at": "2026-06-01",
                "question": "What does the equipment inspection report state?",
            }
            # The original fixture warmed both caches; new authority must invalidate that answer.
            unsynchronized = client.post(
                "/api/v1/query",
                json=query | {"question": "Prepare the DSCR underwriting packet"},
                headers=user_headers,
            )
            assert unsynchronized.status_code == 503, unsynchronized.text
            assert unsynchronized.json()["error"]["code"] == "search_index_incomplete"
            index_current_scope(client, settings, actor.subject)
            answer = client.post("/api/v1/query", json=query, headers=user_headers)
            assert answer.status_code == 200, answer.text
            packet = Packet.model_validate(answer.json())
            assert packet.synthesis is not None and packet.synthesis.status == "answered"
            assert "lathe" in " ".join(s.text.lower() for s in packet.synthesis.statements)
            admitted_chunks = [
                c for c in packet.evidence if c.document_id == source.pages[0].document_id
            ]
            assert admitted_chunks and not packet.cache_hit
            inspected = client.get(
                "/api/v1/evidence/" + admitted_chunks[0].chunk_id,
                params={"borrower_id": "borrower-001", "effective_at": "2026-06-01"},
                headers=user_headers,
            )
            assert inspected.status_code == 200, inspected.text
    finally:
        with store.engine.begin() as connection:
            connection.execute(delete(audit_events).where(audit_events.c.subject == actor.subject))
            connection.execute(delete(grants).where(grants.c.subject == actor.subject))


def stage_inspection_pdf(directory: Path, source_root: Path) -> IngestionInput:
    """Use actual fixture PDF bytes; the worker must replace pending metadata text by parsing."""
    text = "The equipment inspection report states that the lathe passed inspection."
    page = borrower_pages(1)[0].model_copy(
        update={
            "document_id": "inspection-" + uuid4().hex,
            "document_kind": "inspection_report",
            "page": 1,
            "title": "Equipment inspection report",
            "section": "Inspection outcome",
            "text": text,
            "content_hash": text_hash(text),
        }
    )
    pdf = directory / "inspection.pdf"
    _write_pdf(pdf, (page,))
    digest = LocalSourceStore(source_root).stage(page.tenant_id, pdf.read_bytes())
    return IngestionInput(source_sha256=digest, pages=(page,), parser="digital")


def index_current_scope(client: TestClient, settings: Settings, subject: str) -> None:
    """Use the real scoped indexing command with a separate writer credential and pinned models."""
    models = LocalNeuralRanker(Path(settings.local_model_directory))
    try:
        with httpx.Client(
            verify=ssl.create_default_context(cafile=settings.weaviate_ca_file), trust_env=False
        ) as transport:
            vectors = WeaviateStore(
                settings.weaviate_url,
                settings.weaviate_collection,
                transport,
                namespace=settings.governed_catalog_id,
                revision=models.revision,
                token=SecretStr(os.environ["CREDITLENS_WEAVIATE_ADMIN_KEY"]),
                timeout_seconds=30,
            )
            synchronize(
                client.app.state.workflow.catalog,
                client.app.state.store,
                vectors,
                models,
                subject,
                "borrower-001",
                date(2026, 6, 1),
            )
    finally:
        models.close()
    if settings.production_lexical == "opensearch":
        from tests.governed_opensearch_live import synchronize_lexical_scope

        synchronize_lexical_scope(
            settings, client.app.state.workflow.catalog, client.app.state.store, subject
        )
