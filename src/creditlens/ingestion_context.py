"""API and operator workers select the same canonical ingestion authority."""

from sqlalchemy.engine import Engine

from creditlens.ingestion_jobs import JobStore
from creditlens.settings import Settings
from creditlens.sql_catalog import SqlEvidenceCatalog


def ingestion_context(config: Settings, engine: Engine) -> tuple[JobStore, SqlEvidenceCatalog]:
    """Require existing state; governed workers never create a catalog or seed demo evidence."""
    if not config.ingestion_enabled or config.catalog_backend != "postgres":
        raise ValueError("Enable the configured PostgreSQL ingestion path first")
    catalog_id = (
        config.governed_catalog_id if config.mode == "production" else config.demo_catalog_id
    )
    catalog = SqlEvidenceCatalog(engine, catalog_id)
    store = JobStore(
        engine,
        config.ingestion_queue_id,
        catalog=catalog if config.mode == "production" else None,
    )
    return store, catalog
