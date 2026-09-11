"""Run the public demonstration with an immutable synthetic-data configuration."""

import os
from pathlib import Path

import uvicorn
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from creditlens.api import create_app
from creditlens.settings import Settings


class PublicDemoSettings(Settings):
    """Public container configuration comes only from reviewed initialization and fixed defaults."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Ignore ambient sources so synthetic serving cannot be redirected."""
        return (init_settings,)


def demo_settings(mode: str = "lexical") -> Settings:
    """Expose two reviewed variants; neural weights use the read-only /models mount."""
    if mode not in {"lexical", "hybrid"}:
        raise ValueError("The public demo supports only lexical or hybrid retrieval")
    return PublicDemoSettings(
        mode="demo",
        database_url="sqlite:////app/data/creditlens.db",
        cors_origins=[],
        retrieval_mode="hybrid" if mode == "hybrid" else "lexical",
        local_model_directory="/models" if mode == "hybrid" else "",
    )


def main() -> None:
    """Pin mode and database locally so Space variables cannot redirect the demo to real data."""
    data = Path("/app/data")
    data.mkdir(parents=True, exist_ok=True)
    settings = demo_settings(os.environ.get("CREDITLENS_DEMO_RETRIEVAL", "lexical"))
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",  # noqa: S104 - the container's declared public HTTP interface
        port=7860,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
