"""Versioned HTTP boundary for authorized underwriting evidence."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from creditlens import __version__
from creditlens.auth import Authenticator
from creditlens.domain import Borrower, Principal, StrictModel
from creditlens.errors import ServiceError
from creditlens.settings import Settings
from creditlens.storage import GrantStore, demo_borrowers, open_database


class BorrowerList(StrictModel):
    """Mode and scoped choices make the synthetic boundary visible to the browser."""

    borrowers: tuple[Borrower, ...]
    mode: str


def create_app(settings: Settings | None = None) -> FastAPI:
    """An application factory keeps configuration and dependency ownership testable."""
    config = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Open resources once and always close them when server or tests shut down."""
        engine = open_database(config.database_url)
        store = GrantStore(engine)
        if config.mode == "demo":
            store.seed_demo()
        app.state.store = store
        app.state.auth = Authenticator(config, store)
        with httpx.Client(timeout=config.request_timeout_seconds, follow_redirects=False) as client:
            app.state.http = client
            try:
                yield
            finally:
                engine.dispose()

    app = FastAPI(title="CreditLens", version=__version__, lifespan=lifespan)
    app.state.settings = config
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.add_exception_handler(ServiceError, service_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(SQLAlchemyError, storage_error)
    app.get("/health")(health)
    app.get("/ready")(ready)
    app.get("/api/v1/borrowers", response_model=BorrowerList)(borrowers)
    frontend = Path(__file__).resolve().parents[2] / "apps" / "web" / "dist"
    if frontend.is_dir():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="web")
    return app


async def service_error(request: Request, exc: Exception) -> JSONResponse:
    """Only curated error fields leave the server; provider bodies remain private."""
    if not isinstance(exc, ServiceError):
        raise TypeError("Unexpected error handler input")
    headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
    return JSONResponse(
        {"error": {"code": exc.code, "message": exc.message}},
        status_code=exc.status,
        headers=headers,
    )


async def validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Validation responses omit rejected input so questions and credentials are not echoed."""
    return JSONResponse(
        {"error": {"code": "invalid_request", "message": "Request validation failed"}},
        status_code=422,
    )


async def storage_error(request: Request, exc: Exception) -> JSONResponse:
    """Database details may contain connection secrets and must not become public errors."""
    return JSONResponse(
        {"error": {"code": "storage_unavailable", "message": "Storage is unavailable"}},
        status_code=503,
    )


def current_principal(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> Principal:
    """Resolve current grants per endpoint, independently of browser-provided scope."""
    auth: Authenticator = request.app.state.auth
    return auth.authenticate(authorization)


def health() -> dict[str, str]:
    """Process health deliberately does not call dependencies or imply readiness."""
    return {"status": "ok", "version": __version__}


def ready(request: Request) -> dict[str, str]:
    """Probe required dependencies; a local pass cannot imply a successful cloud deployment."""
    config: Settings = request.app.state.settings
    store: GrantStore = request.app.state.store
    with store.engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    if config.mode == "production":
        probe_cortex(request.app.state.http, config)
    raise ServiceError("workflow_not_initialized", "Query workflow is not initialized")


def probe_cortex(client: httpx.Client, config: Settings) -> None:
    """A zero-content tenant filter verifies connectivity without exposing borrower evidence."""
    try:
        response = client.post(
            config.cortex_url,
            headers={"Authorization": f"Bearer {config.cortex_token.get_secret_value()}"},
            json={
                "query": "readiness",
                "limit": 1,
                "columns": ["CHUNK_ID"],
                "filter": {"@eq": {"TENANT_ID": "__readiness_probe__"}},
            },
        )
        response.raise_for_status()
        if not isinstance(response.json().get("results"), list):
            raise ValueError("Invalid search response")
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        raise ServiceError("search_unavailable", "Search is unavailable") from exc


def borrowers(
    request: Request, principal: Annotated[Principal, Depends(current_principal)]
) -> BorrowerList:
    """Only the demo catalog exists in this slice; production catalog is a separate contract."""
    config: Settings = request.app.state.settings
    return BorrowerList(
        borrowers=demo_borrowers(principal) if config.mode == "demo" else (), mode=config.mode
    )
