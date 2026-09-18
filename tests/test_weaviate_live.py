"""Opt-in actual PostgreSQL, Weaviate and pinned-model integration using synthetic evidence."""

import os
from datetime import date
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete
from sqlalchemy.engine import make_url

from creditlens.corpus import build_demo_pages
from creditlens.domain import QueryRequest
from creditlens.errors import ServiceError
from creditlens.neural_search import LocalNeuralRanker
from creditlens.retrieval import chunk_page
from creditlens.sql_catalog import chunks, initialize_catalog, page_acls, pages, states
from creditlens.storage import GrantStore, audit_events, grants, open_database
from creditlens.weaviate_provider import WeaviateHybridProvider, WeaviateProvider
from creditlens.weaviate_store import WeaviateStore
from creditlens.workflow import QueryWorkflow


@pytest.mark.skipif(
    os.getenv("CREDITLENS_TEST_VECTOR_POSTGRES_URL") is None,
    reason="explicit local vector integration database and model bundle required",
)
def test_live_governed_vectors():
    """Real vector ranking preserves packet outcomes, stale-index denial and SQL revocation."""
    url = os.environ["CREDITLENS_TEST_VECTOR_POSTGRES_URL"]
    parsed = make_url(url)
    if parsed.host != "127.0.0.1" or parsed.database != "creditlens_vector_test":
        raise ValueError("Use the isolated loopback creditlens_vector_test database")
    model_directory = Path(os.environ["CREDITLENS_TEST_VECTOR_MODELS"])
    engine = open_database(url)
    catalog_id = "vector-test-" + uuid4().hex
    collection = "CreditLensTest" + uuid4().hex
    subject = "vector-test-" + uuid4().hex
    catalog = initialize_catalog(engine, catalog_id)
    catalog.publish(build_demo_pages())
    store = GrantStore(engine)
    with engine.begin() as connection:
        connection.execute(
            grants.insert().values(
                subject=subject,
                tenant_id="demo-bank",
                role="underwriter",
                borrower_ids=[f"borrower-{index:03}" for index in range(1, 6)],
                acl_groups=["underwriting"],
                revision=1,
                enabled=True,
            )
        )
    principal = store.resolve(subject)
    models = LocalNeuralRanker(model_directory)
    created = False
    with httpx.Client(trust_env=False) as client:
        vectors = WeaviateStore(
            "http://127.0.0.1:18081",
            collection,
            client,
            namespace=catalog_id,
            revision=models.revision,
            allow_local_http=True,
            timeout_seconds=30,
        )
        try:
            vectors.create()
            created = True
            all_chunks = tuple(c for page in build_demo_pages() for c in chunk_page(page))
            for offset in range(0, len(all_chunks), 100):
                batch = all_chunks[offset : offset + 100]
                vectors.upsert(batch, models.encode_documents(batch))
            # Repeating a batch must replace the same objects, not introduce duplicates.
            vectors.upsert(all_chunks[:1], models.encode_documents(all_chunks[:1]))
            provider = WeaviateHybridProvider(catalog, store, vectors, models)
            workflow = QueryWorkflow(catalog, store, provider)
            baseline = QueryWorkflow(catalog, store)
            for borrower in principal.borrower_ids:
                request = QueryRequest(
                    borrower_id=borrower,
                    question="Prepare the DSCR underwriting packet",
                    effective_at=date(2026, 9, 1),
                )
                actual = workflow.query(request, principal)
                expected = baseline.query(request, principal)
                assert actual.policy_disposition == expected.policy_disposition
                assert actual.calculated_metrics == expected.calculated_metrics
                assert all(
                    c.borrower_id in (None, borrower)
                    and c.tenant_id == principal.tenant_id
                    and "underwriting" in c.acl_groups
                    for c in actual.evidence
                )
            request = QueryRequest(
                borrower_id="borrower-001",
                question="minimum DSCR policy",
                effective_at=date(2026, 9, 1),
            )
            dense = WeaviateProvider(catalog, store, vectors, models)
            before = dense.search(request, principal)
            assert before.chunks
            with pytest.raises(ServiceError, match="access_denied"):
                dense.search(request.model_copy(update={"borrower_id": "borrower-151"}), principal)
            revoked = before.chunks[0]
            catalog.revoke(revoked.chunk_id)
            after = dense.search(request, principal)
            assert revoked not in after.chunks
            with pytest.raises(ServiceError, match="evidence_changed"):
                dense.verify(before)
            # New canonical evidence must force synchronization before an answer is returned.
            source = before.chunks[-1]
            extra = next(
                page
                for page in build_demo_pages()
                if (page.document_id, page.document_version, page.page)
                == (source.document_id, source.document_version, source.page)
            ).model_copy(update={"document_id": "new-vector-evidence"})
            catalog.publish((extra,))
            with pytest.raises(ServiceError, match="search_index_incomplete"):
                dense.search(request, principal)
        finally:
            models.close()
            if created:
                client.delete(f"http://127.0.0.1:18081/v1/schema/{collection}").raise_for_status()
            with engine.begin() as connection:
                connection.execute(delete(audit_events).where(audit_events.c.subject == subject))
                connection.execute(delete(grants).where(grants.c.subject == subject))
                connection.execute(delete(chunks).where(chunks.c.catalog_id == catalog_id))
                connection.execute(delete(page_acls).where(page_acls.c.catalog_id == catalog_id))
                connection.execute(delete(pages).where(pages.c.catalog_id == catalog_id))
                connection.execute(delete(states).where(states.c.catalog_id == catalog_id))
            engine.dispose()
