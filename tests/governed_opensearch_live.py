"""Optional real lexical service composition for the governed generation integration."""

import os
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from uuid import uuid4

import httpx
from pydantic import SecretStr
from sqlalchemy import delete

from creditlens.governed_opensearch import GovernedOpenSearchProvider
from creditlens.opensearch_indexing import synchronize
from creditlens.settings import Settings
from creditlens.sql_catalog import SqlEvidenceCatalog
from creditlens.storage import GrantStore, grants


def synchronize_lexical_scope(
    settings: Settings, catalog: SqlEvidenceCatalog, store: GrantStore, subject: str
) -> None:
    """Use the real administrative writer after publication with a distinct scoped credential."""
    with httpx.Client(
        verify=ssl.create_default_context(cafile=settings.opensearch_ca_file), trust_env=False
    ) as client:
        writer = GovernedOpenSearchProvider(
            settings.opensearch_url,
            settings.opensearch_index,
            client,
            catalog,
            store,
            namespace=catalog.catalog_id,
            authority=catalog.authority_id,
            token=SecretStr(os.environ["CREDITLENS_TEST_OPENSEARCH_WRITER_TOKEN"]),
            timeout_seconds=30,
        )
        synchronize(catalog, store, writer, subject, "borrower-001", date(2026, 6, 1))


@contextmanager
def lexical_fixture(
    settings: Settings, catalog: SqlEvidenceCatalog, store: GrantStore, subject: str
) -> Iterator[Settings]:
    """Provision only an isolated test index; the actual API receives a read-only credential."""
    endpoint = os.getenv("CREDITLENS_TEST_OPENSEARCH_URL", "")
    if not endpoint:
        yield settings
        return
    index_name = "creditlens-rag-" + uuid4().hex
    ca = os.environ["CREDITLENS_TEST_OPENSEARCH_CA"]
    writer_token = os.environ["CREDITLENS_TEST_OPENSEARCH_WRITER_TOKEN"]
    configured = Settings(
        **(
            settings.model_dump()
            | {
                "production_lexical": "opensearch",
                "opensearch_url": endpoint,
                "opensearch_index": index_name,
                "opensearch_ca_file": ca,
                "opensearch_token": SecretStr(
                    os.environ["CREDITLENS_TEST_OPENSEARCH_READER_TOKEN"]
                ),
            }
        )
    )
    principal = store.resolve(subject)
    admin = "lexical-admin-" + uuid4().hex
    with store.engine.begin() as connection:
        connection.execute(
            grants.insert().values(
                subject=admin,
                tenant_id=principal.tenant_id,
                role="admin",
                borrower_ids=list(principal.borrower_ids),
                acl_groups=list(principal.acl_groups),
                revision=1,
                enabled=True,
            )
        )
    with httpx.Client(verify=ssl.create_default_context(cafile=ca), trust_env=False) as client:
        attempted = False
        try:
            writer = GovernedOpenSearchProvider(
                endpoint,
                index_name,
                client,
                catalog,
                store,
                namespace=catalog.catalog_id,
                authority=catalog.authority_id,
                token=SecretStr(writer_token),
                timeout_seconds=30,
            )
            for ordinal, borrower in enumerate(principal.borrower_ids):
                attempted = True
                synchronize(
                    catalog,
                    store,
                    writer,
                    admin,
                    borrower,
                    date(2026, 6, 1),
                    create_index=ordinal == 0,
                )
            yield configured
        finally:
            if attempted:
                response = client.delete(
                    endpoint + "/" + index_name,
                    headers={"Authorization": "Bearer " + writer_token},
                )
                if response.status_code != 404:
                    response.raise_for_status()
            with store.engine.begin() as connection:
                connection.execute(delete(grants).where(grants.c.subject == admin))
