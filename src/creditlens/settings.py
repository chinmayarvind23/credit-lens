"""Validate deployment modes before dependencies can expose data."""

from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    redis_url: SecretStr = SecretStr("")
    cache_signing_key: SecretStr = SecretStr("")
    cache_ttl_seconds: int = 60

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
