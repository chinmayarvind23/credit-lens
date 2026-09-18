"""Opt-in production composition with real PostgreSQL, TLS Weaviate, embeddings and Ollama."""

import json
import os
import socket
import ssl
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Thread
from uuid import uuid4

import httpx
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url

from creditlens.api import create_app
from creditlens.corpus import build_demo_pages
from creditlens.domain import Packet, QueryRequest
from creditlens.ingestion_jobs import initialize_jobs
from creditlens.neural_search import LocalNeuralRanker
from creditlens.retrieval import chunk_page
from creditlens.settings import Settings
from creditlens.sql_catalog import chunks, initialize_catalog, page_acls, pages, states
from creditlens.storage import GrantStore, audit_events, grants, open_database
from creditlens.weaviate_store import WeaviateStore
from creditlens.workflow import QueryWorkflow


@pytest.mark.skipif(
    os.getenv("CREDITLENS_TEST_GENERATION_LIVE") != "1",
    reason="explicit isolated actual production RAG services required",
)
def test_actual_production_rag() -> None:
    """Exercise the factory's full production path; only the identity issuer uses a test key."""
    url = os.environ["CREDITLENS_TEST_VECTOR_POSTGRES_URL"]
    parsed = make_url(url)
    if parsed.host != "127.0.0.1" or parsed.database != "creditlens_vector_test":
        raise ValueError("Use the isolated loopback vector integration database")
    engine = open_database(url)
    catalog_id, subject = "rag-" + uuid4().hex, "rag-" + uuid4().hex
    collection = "RagTest" + uuid4().hex
    catalog = initialize_catalog(engine, catalog_id)
    catalog.publish(build_demo_pages())
    ingestion_enabled = os.getenv("CREDITLENS_TEST_INGESTION_LIVE") == "1"
    if ingestion_enabled:
        initialize_jobs(engine)
    store = GrantStore(engine)
    with engine.begin() as connection:
        connection.execute(
            grants.insert().values(
                subject=subject,
                tenant_id="demo-bank",
                role="underwriter",
                borrower_ids=[f"borrower-{i:03}" for i in range(1, 6)],
                acl_groups=["underwriting"],
                revision=1,
                enabled=True,
            )
        )
    principal = store.resolve(subject)
    certificate = os.environ["CREDITLENS_TEST_VECTOR_CA"]
    settings = Settings(
        mode="production",
        database_url=url,
        catalog_backend="postgres",
        governed_catalog_id=catalog_id,
        production_search="weaviate",
        issuer="https://cognito-idp.us-east-1.amazonaws.com/isolated-fixture",
        client_id="fixture",
        weaviate_url="https://127.0.0.1:18443",
        weaviate_ca_file=certificate,
        weaviate_token=SecretStr(os.environ["CREDITLENS_WEAVIATE_READER_KEY"]),
        weaviate_collection=collection,
        request_timeout_seconds=30,
        local_model_directory=os.environ["CREDITLENS_TEST_VECTOR_MODELS"],
        generation_model=os.environ["CREDITLENS_TEST_GENERATION_MODEL"],
        generation_digest=os.environ["CREDITLENS_TEST_GENERATION_DIGEST"],
        generation_timeout_seconds=180,
        response_cache_enabled=True,
        telemetry_enabled=True,
        redis_url=SecretStr(os.environ["CREDITLENS_TEST_GENERATION_REDIS_URL"]),
        cache_signing_key=SecretStr(os.environ["CREDITLENS_TEST_GENERATION_CACHE_KEY"]),
        ingestion_enabled=ingestion_enabled,
        ingestion_queue_id="governed-" + uuid4().hex,
    )
    with httpx.Client(
        verify=ssl.create_default_context(cafile=certificate), trust_env=False
    ) as client:
        created = False
        try:
            models = LocalNeuralRanker(Path(settings.local_model_directory))
            try:
                vectors = WeaviateStore(
                    settings.weaviate_url,
                    collection,
                    client,
                    namespace=catalog_id,
                    revision=models.revision,
                    token=SecretStr(os.environ["CREDITLENS_WEAVIATE_ADMIN_KEY"]),
                    timeout_seconds=30,
                )
                vectors.create()
                created = True
                evidence = tuple(c for page in build_demo_pages() for c in chunk_page(page))
                for offset in range(0, len(evidence), 100):
                    batch = evidence[offset : offset + 100]
                    vectors.upsert(batch, models.encode_documents(batch))
            finally:
                models.close()
            from tests.governed_opensearch_live import lexical_fixture

            with lexical_fixture(settings, catalog, store, principal.subject) as configured:
                exercise_production_app(
                    configured, principal.subject, QueryWorkflow(catalog, store)
                )
        finally:
            if created:
                client.delete(
                    settings.weaviate_url + f"/v1/schema/{collection}",
                    headers={
                        "Authorization": "Bearer " + os.environ["CREDITLENS_WEAVIATE_ADMIN_KEY"]
                    },
                ).raise_for_status()
            with engine.begin() as connection:
                connection.execute(delete(audit_events).where(audit_events.c.subject == subject))
                connection.execute(delete(grants).where(grants.c.subject == subject))
                for table in (chunks, page_acls, pages, states):
                    connection.execute(delete(table).where(table.c.catalog_id == catalog_id))
            engine.dispose()


def exercise_production_app(settings: Settings, subject: str, baseline: QueryWorkflow) -> None:
    """Signed access tokens enter real HTTP handlers with all business dependencies unmocked."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": subject,
            "iss": settings.issuer,
            "iat": now,
            "exp": now + timedelta(minutes=15),
            "token_use": "access",
            "client_id": settings.client_id,
            "scope": settings.required_scope,
        },
        key,
        algorithm="RS256",
    )
    with TestClient(create_app(settings)) as client:
        client.app.state.auth.key_resolver = lambda value: key.public_key()
        assert client.get("/ready").status_code == 200
        principal = baseline.store.resolve(subject)
        for borrower in principal.borrower_ids:
            request = QueryRequest(
                borrower_id=borrower,
                question="Prepare the DSCR underwriting packet",
                effective_at=date(2026, 6, 1),
            )
            assert (
                client.post("/api/v1/query", json=request.model_dump(mode="json")).status_code
                == 401
            )
            response = client.post(
                "/api/v1/query",
                json=request.model_dump(mode="json"),
                headers={"Authorization": "Bearer " + token},
            )
            assert response.status_code == 200, response.text
            packet = Packet.model_validate(response.json())
            expected = baseline.query(request, principal)
            assert packet.calculated_metrics == expected.calculated_metrics
            assert packet.policy_disposition == expected.policy_disposition
            assert packet.recommended_next_actions == expected.recommended_next_actions
            if expected.abstained:
                assert packet.synthesis is None and packet.provider_mode == "rag-withheld"
            else:
                assert packet.synthesis is not None and packet.synthesis.status == "answered"
                assert packet.provider_mode == "ollama-rag"
                assert packet.synthesis.model_digest == settings.generation_digest
                assert packet.synthesis.output_tokens > 0
                assert any(s.name == "generation.synthesize" for s in packet.stages)
                repeat = client.post(
                    "/api/v1/query",
                    json=request.model_dump(mode="json"),
                    headers={"Authorization": "Bearer " + token},
                )
                repeated = Packet.model_validate(repeat.json())
                assert repeated.cache_hit and repeated.synthesis == packet.synthesis
                assert not any(s.name == "generation.synthesize" for s in repeated.stages)
                if borrower == "borrower-001":
                    cache = client.app.state.workflow.response_cache
                    client.app.state.workflow.response_cache = None
                    try:
                        warm = client.post(
                            "/api/v1/query",
                            json=request.model_dump(mode="json"),
                            headers={"Authorization": "Bearer " + token},
                        )
                        assert warm.status_code == 200, warm.text
                        assert any(
                            s["name"] == "cache.retrieval.hit" for s in warm.json()["stages"]
                        )
                        assert warm.json()["synthesis"]["status"] == "answered"
                    finally:
                        client.app.state.workflow.response_cache = cache
            with baseline.store.engine.connect() as connection:
                event = connection.execute(
                    select(audit_events.c.event).where(
                        audit_events.c.request_id == packet.request_id
                    )
                ).scalar_one()
                assert "weaviate" in event["search_provider_mode"]
                if settings.production_lexical == "opensearch":
                    assert "opensearch-governed-bm25-v1" in event["search_provider_mode"]
                assert (
                    event["protected_packet"]["synthesis"]
                    == packet.model_dump(mode="json")["synthesis"]
                )
        denied = client.post(
            "/api/v1/query",
            json=request.model_copy(update={"borrower_id": "borrower-151"}).model_dump(mode="json"),
            headers={"Authorization": "Bearer " + token},
        )
        assert denied.status_code == 403
        if settings.ingestion_enabled:
            from tests.governed_ingestion_live import exercise_governed_ingestion

            exercise_governed_ingestion(client, settings, key, token, subject)
        if os.getenv("CREDITLENS_VISUAL_GATE"):
            hold_visual_app(client.app, token)


def hold_visual_app(app: FastAPI, token: str) -> None:
    """An explicit local visual drill reuses the live fixture without recreating its resources."""
    gate = Path(os.environ["CREDITLENS_VISUAL_GATE"]).resolve()
    resources = Path(__file__).resolve().parents[2] / "resources" / "credit_lens"
    if not gate.is_relative_to(resources) or gate.exists() or gate.with_suffix(".stop").exists():
        raise ValueError("Use a fresh external CreditLens resource gate")
    token_file = gate.with_suffix(".token")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, lifespan="off", access_log=False, log_level="error")
    )
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        while not server.started:
            if not thread.is_alive() or time.monotonic() - started > 15:
                raise RuntimeError("Visual fixture server did not start")
            time.sleep(0.1)
        token_file.write_text(token, encoding="utf-8")
        gate.write_text(
            json.dumps({"url": f"http://127.0.0.1:{port}", "token_file": str(token_file)}),
            encoding="utf-8",
        )
        while not gate.with_suffix(".stop").exists():
            if time.monotonic() - started > 360:
                raise RuntimeError("Visual verification window expired")
            time.sleep(0.2)
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()
        token_file.unlink(missing_ok=True)
        if thread.is_alive():
            raise RuntimeError("Visual fixture server did not stop")
