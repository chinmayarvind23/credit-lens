"""Public serving must remain synthetic even when ambient settings contain real providers."""

# ruff: noqa: S101
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def entrypoint() -> ModuleType:
    """Load container-only configuration without starting a server or touching /app/data."""
    source = Path(__file__).resolve().parents[1] / "demo_entrypoint.py"
    spec = importlib.util.spec_from_file_location("demo_entrypoint", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("mode", ["lexical", "hybrid"])
def test_public_config_ignores_ambient_providers(
    entrypoint: ModuleType, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Invalid external settings must not redirect or prevent the fixed public configuration."""
    for name in (
        "MODE",
        "DATABASE_URL",
        "CORTEX_URL",
        "CORTEX_TOKEN",
        "REDIS_URL",
        "CACHE_SIGNING_KEY",
        "CATALOG_BACKEND",
        "RETRIEVAL_MODE",
        "LOCAL_MODEL_DIRECTORY",
        "INGESTION_ENABLED",
        "INGESTION_SQS_ENDPOINT",
        "INGESTION_SQS_QUEUE_URL",
        "DEMO_CATALOG_ID",
        "CORS_ORIGINS",
    ):
        monkeypatch.setenv("CREDITLENS_" + name, "invalid-ambient-value")
    settings = entrypoint.demo_settings(mode)
    assert settings.mode == "demo"
    assert settings.database_url == "sqlite:////app/data/creditlens.db"
    assert settings.catalog_backend == "memory"
    assert not settings.ingestion_enabled
    assert not settings.ingestion_sqs_endpoint
    assert not settings.cortex_url
    assert not settings.cortex_token.get_secret_value()
    assert not settings.redis_url.get_secret_value()
    assert settings.cors_origins == []
    assert settings.retrieval_mode == mode
    assert settings.local_model_directory == ("/models" if mode == "hybrid" else "")


def test_public_config_rejects_unknown_variant(entrypoint: ModuleType) -> None:
    """A misspelled selection fails startup instead of silently using another retrieval mode."""
    with pytest.raises(ValueError, match="only lexical or hybrid"):
        entrypoint.demo_settings("production")
