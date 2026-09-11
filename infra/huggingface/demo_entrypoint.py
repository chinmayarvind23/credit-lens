"""Run the public demonstration with an immutable synthetic-data configuration."""

from pathlib import Path

import uvicorn

from creditlens.api import create_app
from creditlens.settings import Settings


def main() -> None:
    """Pin mode and database locally so Space variables cannot redirect the demo to real data."""
    data = Path("/app/data")
    data.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        mode="demo",
        database_url="sqlite:////app/data/creditlens.db",
        cors_origins=[],
    )
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",  # noqa: S104 - the container's declared public HTTP interface
        port=7860,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
