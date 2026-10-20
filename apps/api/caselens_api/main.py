"""FastAPI application factory. `app` is the uvicorn entry point (`caselens_api.main:app`)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout

from caselens_api import __version__
from caselens_api import models as m
from caselens_api.auth import TokenVerifier, build_verifier
from caselens_api.db import Database
from caselens_api.deps import RateLimiter, Services
from caselens_api.problems import ProblemError, install_problem_handlers, problem_response
from caselens_api.routes import agent, audit, cases, evidence, graph, ingest
from caselens_api.settings import Settings, get_settings
from caselens_api.storage import EvidenceStore, S3EvidenceStore


def create_app(
    settings: Settings | None = None,
    *,
    verifier: TokenVerifier | None = None,
    database: Database | None = None,
    store_factory: Callable[[], EvidenceStore] | None = None,
    ask_kwargs: dict[str, Any] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if database is None and settings.database_url:
        database = Database(
            settings.database_url,
            settings.app_db_role,
            settings.readonly_db_role,
            settings.db_pool_min_size,
            settings.db_pool_max_size,
        )
    services = Services(
        settings=settings,
        verifier=verifier or build_verifier(settings),
        db=database,
        store_factory=store_factory or (lambda: S3EvidenceStore(settings)),
        ask_limiter=RateLimiter(settings.ask_rate_limit, settings.ask_rate_window_s),
        ask_kwargs=dict(ask_kwargs or {}),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if services.db is not None:
            services.db.open()
        try:
            yield
        finally:
            if services.db is not None:
                services.db.close()

    app = FastAPI(
        title="CaseLens API",
        version=__version__,
        summary="Evidence-search copilot API with cited, auditable answers (SYNTHETIC data).",
        lifespan=lifespan,
    )
    app.state.services = services
    install_problem_handlers(app)

    async def on_db_unavailable(request: Request, exc: Exception) -> JSONResponse:
        return problem_response(request, ProblemError(503, "The database is unavailable."))

    app.add_exception_handler(PoolTimeout, on_db_unavailable)
    app.add_exception_handler(psycopg.OperationalError, on_db_unavailable)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
        expose_headers=["Location", "Retry-After"],
        max_age=600,
    )

    @app.get("/healthz", response_model=m.Health)
    def healthz() -> m.Health:
        return m.Health(status="ok", version=__version__)

    for module in (cases, evidence, ingest, agent, graph, audit):
        app.include_router(module.router)
    return app


app = create_app()
