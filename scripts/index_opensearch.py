"""Synchronize an existing governed SQL scope using a separately configured index writer."""

import argparse
from datetime import date
from ssl import create_default_context

import httpx

from creditlens.governed_opensearch import GovernedOpenSearchProvider
from creditlens.opensearch_indexing import synchronize
from creditlens.settings import Settings
from creditlens.sql_catalog import SqlEvidenceCatalog
from creditlens.storage import GrantStore, open_database


def main() -> None:
    """Require explicit administrator scope and never initialize SQL or delete an index."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--borrower", required=True)
    parser.add_argument("--effective-at", type=date.fromisoformat, required=True)
    parser.add_argument("--create-index", action="store_true")
    args = parser.parse_args()
    config = Settings()
    if config.mode != "production" or config.production_lexical != "opensearch":
        parser.error("Configure the governed OpenSearch production path first")
    engine = open_database(config.database_url)
    try:
        grants = GrantStore(engine)
        catalog = SqlEvidenceCatalog(engine, config.governed_catalog_id)
        with httpx.Client(
            trust_env=False,
            follow_redirects=False,
            verify=create_default_context(cafile=config.opensearch_ca_file or None),
        ) as client:
            index = GovernedOpenSearchProvider(
                config.opensearch_url,
                config.opensearch_index,
                client,
                catalog,
                grants,
                namespace=catalog.catalog_id,
                authority=catalog.authority_id,
                token=config.opensearch_token,
                timeout_seconds=config.request_timeout_seconds,
            )
            synchronize(
                catalog,
                grants,
                index,
                args.subject,
                args.borrower,
                args.effective_at,
                create_index=args.create_index,
            )
        print("Authorized evidence scope synchronized.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
