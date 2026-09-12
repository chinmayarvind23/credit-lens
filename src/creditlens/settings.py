"""Validate deployment modes before dependencies can expose data."""

from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from creditlens.sqs_queue import queue_origin


class Settings(BaseSettings):
    """Explicit demo isolation prevents a convenient local identity reaching real providers."""

    model_config = SettingsConfigDict(env_prefix="CREDITLENS_", extra="ignore")
    mode: Literal["demo", "production"] = "demo"
    database_url: str = "sqlite:///./data/creditlens.db"
    issuer: str = ""
    client_id: str = ""
    required_scope: str = "creditlens/query"
    cortex_url: str = ""
    cortex_token: SecretStr = SecretStr("")
    cors_origins: list[str] = ["http://localhost:3000"]
    request_timeout_seconds: float = 5.0
    response_cache_enabled: bool = False
    response_cache_capacity: int = Field(default=128, ge=1, le=512)
    response_cache_ttl_seconds: int = Field(default=60, ge=1, le=3600)
    graphql_enabled: bool = False
    telemetry_enabled: bool = False
    trace_file: str = Field(default="", max_length=2048)
    redis_url: SecretStr = SecretStr("")
    cache_signing_key: SecretStr = SecretStr("")
    cache_ttl_seconds: int = 60
    catalog_backend: Literal["memory", "postgres"] = "memory"
    governed_catalog_id: str = Field(default="", pattern=r"^$|^[A-Za-z0-9_-]{1,100}$")
    retrieval_mode: Literal["lexical", "hybrid"] = "lexical"
    local_model_directory: str = Field(default="", max_length=2048)
    ingestion_enabled: bool = False
    ingestion_sqs_endpoint: str = Field(default="", max_length=2048)
    ingestion_sqs_queue_url: str = Field(default="", max_length=2048)
    ingestion_queue_id: str = Field(
        default="synthetic-ingestion-v1", pattern=r"^synthetic-[a-z0-9-]{1,64}$"
    )
    demo_catalog_id: str = Field(
        default="synthetic-demo-v1", pattern=r"^synthetic-[a-z0-9-]{1,64}$"
    )

    @model_validator(mode="after")
    def validate_local_models(self) -> "Settings":
        """Require an explicit offline model directory and preserve production/demo separation."""
        if (self.retrieval_mode == "hybrid") != bool(self.local_model_directory):
            raise ValueError("Hybrid retrieval and a local model directory must be set together")
        if self.retrieval_mode == "hybrid" and self.mode != "demo":
            raise ValueError("Local hybrid workflow currently supports demo mode only")
        return self

    @model_validator(mode="after")
    def validate_catalog(self) -> "Settings":
        """Require an explicit existing catalog before production can use governed evidence."""
        if self.catalog_backend == "postgres" and not self.database_url.startswith(
            "postgresql+psycopg://"
        ):
            raise ValueError("The shared catalog requires PostgreSQL psycopg")
        if self.governed_catalog_id and (
            self.mode != "production" or self.catalog_backend != "postgres"
        ):
            raise ValueError("A governed catalog requires production mode and PostgreSQL")
        if (
            self.mode == "production"
            and self.catalog_backend == "postgres"
            and not self.governed_catalog_id
        ):
            raise ValueError("Production PostgreSQL requires an existing governed catalog ID")
        return self

    @model_validator(mode="after")
    def validate_ingestion(self) -> "Settings":
        """Expose staged synthetic jobs only with the explicitly enabled shared catalog."""
        if self.ingestion_enabled and (self.catalog_backend != "postgres" or self.mode != "demo"):
            raise ValueError("Ingestion requires the shared demo PostgreSQL catalog")
        if bool(self.ingestion_sqs_endpoint) != bool(self.ingestion_sqs_queue_url):
            raise ValueError("SQS endpoint and queue URL must be configured together")
        if self.ingestion_sqs_endpoint:
            if not self.ingestion_enabled:
                raise ValueError("SQS notifications require enabled ingestion")
            queue_origin(self.ingestion_sqs_endpoint, self.ingestion_sqs_queue_url)
        return self

    @model_validator(mode="after")
    def isolate_demo(self) -> "Settings":
        """Fail startup on mixed trust modes instead of silently using synthetic credentials."""
        if self.mode == "demo" and (self.cortex_url or self.cortex_token.get_secret_value()):
            raise ValueError("Demo mode cannot use production search credentials")
        if self.mode == "production":
            if not all(
                (self.issuer, self.client_id, self.cortex_url, self.cortex_token.get_secret_value())
            ):
                raise ValueError("Production requires Cognito and Cortex configuration")
            if not self.issuer.startswith("https://cognito-idp."):
                raise ValueError("Production issuer must be an HTTPS Cognito user pool")
            if not self.cortex_url.startswith("https://"):
                raise ValueError("Cortex must use HTTPS")
        if not 0 < self.request_timeout_seconds <= 30:
            raise ValueError("Request timeout must be between zero and 30 seconds")
        return self

    @model_validator(mode="after")
    def validate_cache(self) -> "Settings":
        """Reject partial secret configuration rather than silently disabling requested caching."""
        url, key = self.redis_url.get_secret_value(), self.cache_signing_key.get_secret_value()
        if bool(url) != bool(key):
            raise ValueError("Redis URL and cache signing key must be configured together")
        if url:
            if len(key.encode()) < 32:
                raise ValueError("Cache signing key must contain at least 32 bytes")
            if self.mode != "demo":
                raise ValueError("Redis workflow integration currently supports demo mode only")
        if not 1 <= self.cache_ttl_seconds <= 3600:
            raise ValueError("Cache TTL must be between 1 and 3600 seconds")
        return self
