"""Ingestion opt-in must preserve governed identity, model and catalog configuration gates."""

import pytest
from pydantic import SecretStr, ValidationError

from creditlens.settings import Settings


def governed_values() -> dict[str, object]:
    """Supply all protected-mode dependencies without connecting to any external service."""
    return {
        "mode": "production",
        "database_url": "postgresql+psycopg://test:test-only@127.0.0.1:15459/creditlens_test",
        "catalog_backend": "postgres",
        "governed_catalog_id": "governed-fixture",
        "ingestion_enabled": True,
        "ingestion_queue_id": "governed-ingestion-fixture",
        "issuer": "https://cognito-idp.us-east-1.amazonaws.com/test",
        "client_id": "test-client",
        "cortex_url": "https://example.invalid/query",
        "cortex_token": SecretStr("test-only"),
        "generation_model": "fixture:local",
        "generation_digest": "a" * 64,
    }


def test_ingestion_is_opt_in_without_changing_demo_defaults() -> None:
    """The ordinary local demo still needs no protected catalog, model or queue service."""
    config = Settings(_env_file=None)
    assert config.mode == "demo"
    assert not config.ingestion_enabled
    assert config.ingestion_queue_id == "synthetic-ingestion-v1"
    assert not config.governed_catalog_id
    assert not config.generation_model


def test_governed_ingestion_accepts_complete_explicit_configuration() -> None:
    """The admin path is enabled only alongside the real production configuration contract."""
    config = Settings(**governed_values())
    assert config.ingestion_enabled and config.mode == "production"
    assert config.governed_catalog_id == "governed-fixture"
    assert config.generation_model == "fixture:local"


@pytest.mark.parametrize(
    "changes",
    [
        {"catalog_backend": "memory"},
        {"database_url": "sqlite:///:memory:"},
        {"governed_catalog_id": ""},
        {"ingestion_queue_id": "synthetic-ingestion-v1"},
        {"ingestion_queue_id": ""},
        {"ingestion_queue_id": "x" * 81},
        {"ingestion_queue_id": "../other"},
        {"issuer": ""},
        {"client_id": ""},
        {"cortex_token": SecretStr("")},
        {"generation_model": "", "generation_digest": ""},
        {"generation_digest": ""},
        {"generation_url": "http://model.example.test:11434"},
    ],
)
def test_ingestion_does_not_bypass_protected_configuration(changes: dict[str, object]) -> None:
    """Opting into publication cannot weaken identity, storage, model or queue boundaries."""
    with pytest.raises(ValidationError):
        Settings(**(governed_values() | changes))


def test_disabled_production_ingestion_does_not_require_a_bound_queue() -> None:
    """Existing governed serving remains usable when no ingestion service is requested."""
    config = Settings(
        **(
            governed_values()
            | {"ingestion_enabled": False, "ingestion_queue_id": "synthetic-ingestion-v1"}
        )
    )
    assert not config.ingestion_enabled
    assert config.governed_catalog_id == "governed-fixture"


@pytest.mark.parametrize("enabled", [False, True])
def test_demo_cannot_select_a_governed_queue(enabled: bool) -> None:
    """Demo identity cannot point at a queue reserved for governed publishers."""
    with pytest.raises(ValidationError, match="synthetic queue"):
        Settings(
            ingestion_enabled=enabled,
            catalog_backend="postgres",
            database_url=governed_values()["database_url"],
            ingestion_queue_id="governed-work",
        )


def test_production_generator_requires_an_actual_governed_workflow() -> None:
    """A configured model must not be silently ignored by an uninitialized production runtime."""
    values = governed_values() | {
        "ingestion_enabled": False,
        "governed_catalog_id": "",
        "catalog_backend": "memory",
        "database_url": "sqlite:///:memory:",
    }
    with pytest.raises(ValidationError, match="Production generation requires a governed catalog"):
        Settings(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"ingestion_sqs_endpoint": "http://127.0.0.1:19324"},
        {"ingestion_sqs_queue_url": "http://127.0.0.1:19324/queue"},
        {
            "ingestion_enabled": False,
            "ingestion_sqs_endpoint": "http://127.0.0.1:19324",
            "ingestion_sqs_queue_url": "http://127.0.0.1:19324/queue",
        },
        {
            "ingestion_sqs_endpoint": "https://sqs.us-east-1.amazonaws.com",
            "ingestion_sqs_queue_url": "https://sqs.us-east-1.amazonaws.com/account/queue",
        },
    ],
)
def test_governed_ingestion_retains_explicit_local_queue_transport(
    changes: dict[str, object],
) -> None:
    """Production catalog support does not authorize partial broker settings or cloud endpoints."""
    with pytest.raises(ValidationError):
        Settings(**(governed_values() | changes))
