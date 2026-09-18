"""Explicit, repeatable synchronization of an operator's authorized canonical evidence scope."""

import argparse
from datetime import date
from pathlib import Path
from ssl import create_default_context

import httpx

from creditlens.errors import ServiceError
from creditlens.neural_search import LocalNeuralRanker
from creditlens.settings import Settings
from creditlens.sql_catalog import SqlEvidenceCatalog
from creditlens.storage import GrantStore, open_database
from creditlens.weaviate_store import WeaviateStore


def synchronize(
    catalog: SqlEvidenceCatalog,
    grants: GrantStore,
    vectors: WeaviateStore,
    models: LocalNeuralRanker,
    subject: str,
    borrower: str,
    effective_at: date,
) -> int:
    """Index only current admin-authorized evidence and detect concurrent revocation."""
    principal = grants.resolve(subject)
    if principal.role != "admin":
        raise ServiceError("access_denied", "Indexing requires an administrator", 403)
    candidates, revision = catalog.snapshot(principal, borrower, effective_at)
    for offset in range(0, len(candidates), 100):
        catalog.verify_revision(revision)
        if grants.resolve(subject) != principal:
            raise ServiceError("access_changed", "Access changed; retry indexing", 409)
        batch = candidates[offset : offset + 100]
        encoded = models.encode_documents(batch)
        catalog.verify_revision(revision)
        if grants.resolve(subject) != principal:
            raise ServiceError("access_changed", "Access changed; retry indexing", 409)
        vectors.upsert(batch, encoded)
    catalog.verify_revision(revision)
    if grants.resolve(subject) != principal:
        raise ServiceError("access_changed", "Access changed; retry indexing", 409)
    return len(candidates)


def main() -> None:
    """Reuse deployment settings; provisioning is explicit and never runs on user queries."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--borrower", required=True)
    parser.add_argument("--effective-at", type=date.fromisoformat, required=True)
    parser.add_argument("--create-collection", action="store_true")
    args = parser.parse_args()
    config = Settings()
    if config.mode != "production" or config.production_search != "weaviate":
        parser.error("Configure the governed Weaviate production path first")
    engine = open_database(config.database_url)
    models = None
    try:
        grants = GrantStore(engine)
        catalog = SqlEvidenceCatalog(engine, config.governed_catalog_id)
        models = LocalNeuralRanker(Path(config.local_model_directory))
        with httpx.Client(
            trust_env=False,
            follow_redirects=False,
            verify=create_default_context(cafile=config.weaviate_ca_file or None),
        ) as client:
            vectors = WeaviateStore(
                config.weaviate_url,
                config.weaviate_collection,
                client,
                namespace=config.governed_catalog_id,
                revision=models.revision,
                token=config.weaviate_token,
                timeout_seconds=config.request_timeout_seconds,
            )
            if args.create_collection:
                vectors.create()
            else:
                vectors.check_ready()
            synchronize(
                catalog, grants, vectors, models, args.subject, args.borrower, args.effective_at
            )
        print("Authorized evidence scope synchronized.")
    finally:
        if models is not None:
            models.close()
        engine.dispose()


if __name__ == "__main__":
    main()
