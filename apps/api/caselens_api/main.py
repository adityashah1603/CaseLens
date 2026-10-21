"""FastAPI application factory. `app` is the uvicorn entry point (`caselens_api.main:app`)."""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout
from starlette.concurrency import run_in_threadpool

from caselens_api import __version__
from caselens_api import models as m
from caselens_api.auth import TokenVerifier, build_verifier
from caselens_api.db import Database
from caselens_api.deps import RateLimiter, Services
from caselens_api.problems import ProblemError, install_problem_handlers, problem_response
from caselens_api.routes import agent, audit, cases, evidence, graph, ingest, storage
from caselens_api.settings import Settings, get_settings
from caselens_api.storage import EvidenceStore, LocalDiskEvidenceStore, S3EvidenceStore
from caselens_api.web import mount_spa

log = logging.getLogger(__name__)


def _warm_up_retrieval() -> None:
    """Best effort: a missing model only costs the first question its load time."""
    try:
        from caselens_retrieval.encoders import warm_up

        warm_up()
    except Exception as exc:
        log.warning("retrieval model warm-up failed: %s", exc)


def default_store_factory(settings: Settings) -> Callable[[], EvidenceStore]:
    if settings.storage_backend == "local":
        return lambda: LocalDiskEvidenceStore.from_settings(settings)
    return lambda: S3EvidenceStore(settings)


def create_app(
    settings: Settings | None = None,
    *,
    verifier: TokenVerifier | None = None,
    database: Database | None = None,
    store_factory: Callable[[], EvidenceStore] | None = None,
    ask_kwargs: dict[str, Any] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    if database is None and settings.pool_url:
        database = Database(
            settings.pool_url,
            settings.app_db_role,
            settings.readonly_db_role,
            settings.db_pool_min_size,
            settings.pool_max_size,
        )
    services = Services(
        settings=settings,
        verifier=verifier or build_verifier(settings),
        db=database,
        store_factory=store_factory or default_store_factory(settings),
        ask_limiter=RateLimiter(settings.ask_rate_limit, settings.ask_rate_window_s),
        ask_slots=threading.BoundedSemaphore(settings.ask_max_concurrent),
        ask_kwargs=dict(ask_kwargs or {}),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if services.db is not None:
            services.db.open()
            try:
                # Outside local the app refuses to start on a superuser / BYPASSRLS login (or
                # one it cannot check); locally it only warns, and does not wait long for a
                # database that is not up yet.
                strict = not settings.is_local
                services.db.check_login(strict=strict, timeout=30.0 if strict else 5.0)
            except Exception:
                services.db.close()
                raise
        if settings.warm_up_models and "backends" not in services.ask_kwargs:
            await run_in_threadpool(_warm_up_retrieval)
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
    if settings.storage_backend == "local":
        app.include_router(storage.router)  # signed-URL emulation, not part of the API contract
    if settings.web_dist is not None:
        mount_spa(app, settings.web_dist)  # last: a catch-all that never shadows the routes above
    return app


app = create_app()
