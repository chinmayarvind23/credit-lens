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
    production_search: Literal["cortex", "weaviate"] = "cortex"
    production_lexical: Literal["local", "opensearch"] = "local"
    opensearch_url: str = ""
    opensearch_index: str = Field(default="", pattern=r"^$|^[a-z][a-z0-9_-]{0,79}$")
    opensearch_token: SecretStr = SecretStr("")
    opensearch_ca_file: str = Field(default="", max_length=2048)
    weaviate_url: str = ""
    weaviate_token: SecretStr = SecretStr("")
    weaviate_ca_file: str = Field(default="", max_length=2048)
    weaviate_collection: str = Field(
        default="CreditLensEvidence", pattern=r"^[A-Z][A-Za-z0-9_]{0,79}$"
    )
    generation_url: str = "http://127.0.0.1:11434"
    generation_model: str = ""
    generation_digest: str = ""
    generation_token: SecretStr = SecretStr("")
    generation_timeout_seconds: float = Field(default=120, ge=1, le=180)
    cors_origins: list[str] = ["http://localhost:3000"]
    request_timeout_seconds: float = 5.0
    response_cache_enabled: bool = False
    response_cache_capacity: int = Field(default=128, ge=1, le=512)
    response_cache_ttl_seconds: int = Field(default=60, ge=1, le=3600)
    graphql_enabled: bool = False
    telemetry_enabled: bool = False
    trace_file: str = Field(default="", max_length=2048)
    redis_url: SecretStr = SecretStr("")
    quota_redis_url: SecretStr = SecretStr("")
    quota_namespace: str = Field(default="creditlens", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    query_limit: int = Field(default=60, ge=1, le=10000)
    query_window_seconds: int = Field(default=60, ge=1, le=3600)
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
        default="synthetic-ingestion-v1", pattern=r"^[A-Za-z0-9_-]{1,80}$"
    )
    demo_catalog_id: str = Field(
        default="synthetic-demo-v1", pattern=r"^synthetic-[a-z0-9-]{1,64}$"
    )

    @model_validator(mode="after")
    def validate_generation(self) -> "Settings":
        """A configured production evidence workflow must include an explicit pinned generator."""
        import re

        from creditlens.opensearch_provider import validate_search_url

        if bool(self.generation_model) != bool(self.generation_digest):
            raise ValueError("Configure the generation model and digest together")
        if self.mode == "production" and self.generation_model and not self.governed_catalog_id:
            raise ValueError("Production generation requires a governed catalog")
        if self.mode == "production" and self.governed_catalog_id and not self.generation_model:
            raise ValueError("Governed production requires a pinned generation model")
        if self.generation_model:
            if (
                not re.fullmatch(r"[a-z0-9._-]+:[a-z0-9._-]+", self.generation_model)
                or "cloud" in self.generation_model
                or not re.fullmatch(r"[a-f0-9]{64}", self.generation_digest)
            ):
                raise ValueError("Expected an installed generation model and SHA256 digest")
            validate_search_url(self.generation_url, "ollama", True)
            if self.generation_url.startswith("https:") != bool(
                self.generation_token.get_secret_value()
            ):
                raise ValueError("Remote generation requires authenticated TLS")
        elif self.generation_token.get_secret_value():
            raise ValueError("Generation credentials require a model")
        return self

    @model_validator(mode="after")
    def validate_local_models(self) -> "Settings":
        """Require an explicit offline model directory and preserve production/demo separation."""
        needs_models = self.retrieval_mode == "hybrid" or self.production_search == "weaviate"
        if needs_models != bool(self.local_model_directory):
            raise ValueError("Hybrid retrieval and a local model directory must be set together")
        if self.retrieval_mode == "hybrid" and self.mode != "demo":
            raise ValueError("Local hybrid workflow currently supports demo mode only")
        return self

    @model_validator(mode="after")
    def validate_vector_search(self) -> "Settings":
        """Select authenticated vector storage only with explicit governed production authority."""
        if self.production_search == "weaviate":
            from creditlens.opensearch_provider import validate_search_url

            if self.mode != "production" or not self.governed_catalog_id:
                raise ValueError("Weaviate requires a governed production catalog")
            validate_search_url(self.weaviate_url, "weaviate", False)
            if not self.weaviate_token.get_secret_value():
                raise ValueError("Weaviate requires an API token")
            if self.cortex_url or self.cortex_token.get_secret_value():
                raise ValueError("Configure only the selected production search service")
        elif self.weaviate_url or self.weaviate_token.get_secret_value() or self.weaviate_ca_file:
            raise ValueError("Weaviate credentials require production_search=weaviate")
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
    def validate_production_lexical(self) -> "Settings":
        """An explicitly selected remote lexical branch cannot be ignored or used by demo mode."""
        configured = bool(
            self.opensearch_url
            or self.opensearch_index
            or self.opensearch_token.get_secret_value()
            or self.opensearch_ca_file
        )
        if self.production_lexical == "opensearch":
            from creditlens.opensearch_provider import validate_search_url

            if self.mode != "production" or self.production_search != "weaviate":
                raise ValueError("OpenSearch requires governed Weaviate production")
            validate_search_url(self.opensearch_url, self.opensearch_index, False)
            if not self.opensearch_token.get_secret_value():
                raise ValueError("OpenSearch requires a bearer token")
        elif configured:
            raise ValueError("OpenSearch configuration requires production_lexical=opensearch")
        return self

    @model_validator(mode="after")
    def validate_ingestion(self) -> "Settings":
        """Governed ingestion requires an existing catalog and a distinct explicitly named queue."""
        if self.ingestion_enabled:
            if self.catalog_backend != "postgres":
                raise ValueError("Ingestion requires the shared PostgreSQL catalog")
            if self.mode == "production" and (
                not self.governed_catalog_id or self.ingestion_queue_id.startswith("synthetic-")
            ):
                raise ValueError("Production ingestion requires a governed catalog and queue")
        if self.mode == "demo" and not self.ingestion_queue_id.startswith("synthetic-"):
            raise ValueError("Demo ingestion requires a synthetic queue")
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
            if not self.issuer or not self.client_id:
                raise ValueError("Production requires Cognito configuration")
            if not self.issuer.startswith("https://cognito-idp."):
                raise ValueError("Production issuer must be an HTTPS Cognito user pool")
            if self.production_search == "cortex":
                if not self.cortex_url or not self.cortex_token.get_secret_value():
                    raise ValueError("Production requires Cognito and Cortex configuration")
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
            if self.mode == "production" and not self.governed_catalog_id:
                raise ValueError("Production retrieval caching requires a governed catalog")
            if len(key.encode()) < 32:
                raise ValueError("Cache signing key must contain at least 32 bytes")
        if not 1 <= self.cache_ttl_seconds <= 3600:
            raise ValueError("Cache TTL must be between 1 and 3600 seconds")
        return self
