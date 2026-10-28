import copy
import hmac
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import psycopg
from asgi_correlation_id import CorrelationIdMiddleware, correlation_id
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tadoss import __version__, metrics, telemetry
from tadoss.adapters import postgres as store
from tadoss.auth import Authentication
from tadoss.domain.types import Document
from tadoss.domain.validation import (
    SPEC,
    APIError,
)
from tadoss.settings import Settings

from .admission import RequestAdmission
from .routes import router

PROBES = {"/healthz", "/readyz"}
logger = logging.getLogger("tadoss")
access_logger = logging.getLogger("tadoss.access")


class AccessLog:
    """Logs method, route template, status and duration; never the query string."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def record(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, record)
        finally:
            route = scope.get("route")
            template = route.path_format if route is not None else scope["path"]
            client = scope.get("client")
            metric_route = route.path_format if route is not None else "unmatched"
            metric_method = (
                scope["method"]
                if scope["method"]
                in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
                else "OTHER"
            )
            metrics.REQUESTS.labels(metric_method, metric_route, str(status)).inc()
            metrics.DURATION.labels(metric_method, metric_route).observe(
                time.perf_counter() - started
            )
            access_logger.log(
                logging.DEBUG if scope["path"] in PROBES else logging.INFO,
                "%s %s %s",
                scope["method"],
                template,
                status,
                extra={
                    "method": scope["method"],
                    "route": template,
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "client": client[0] if client else None,
                },
            )


def published_contract() -> Document:
    contract = copy.deepcopy(SPEC)
    contract["info"]["title"] = "TADOSS"
    contract["info"].pop("contact", None)
    contract["servers"] = [{"url": "/"}]
    return contract


def create_app(
    database: str | None = None,
    token: str | None = None,
    dev: bool | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    settings = settings or Settings.load(database=database, api_token=token, dev=dev)
    database = settings.database_url()
    authentication = Authentication(settings)
    if settings.dev and not (
        settings.api_token or settings.basic_auth_password or settings.oauth2_issuer
    ):
        logger.warning("Authentication disabled in development mode")
    pool = store.create_pool(
        database,
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
        timeout=settings.pool_timeout,
        statement_timeout=settings.statement_timeout,
        configure=lambda connection: telemetry.instrument_connection(connection, app),
    )
    probe_pool = store.create_pool(database, min_size=0, max_size=1, timeout=2)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.database_slots = anyio.CapacityLimiter(settings.pool_max_size)
        app.state.readiness_slots = anyio.CapacityLimiter(1)
        app.state.readiness_threads = anyio.CapacityLimiter(1)
        app.state.metrics_slots = anyio.CapacityLimiter(1)
        app.state.metrics_threads = anyio.CapacityLimiter(1)
        # Connections are established in the background; a request waits up to the
        # pool timeout for one, so a slow database delays readiness instead of
        # crashing the process.
        pool.open(wait=False)
        probe_pool.open(wait=False)
        try:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(metrics.monitor_pool, pool)
                try:
                    yield
                finally:
                    tasks.cancel_scope.cancel()
        finally:
            probe_pool.close()
            pool.close()
            telemetry.shutdown(app)

    docs = settings.docs
    app = FastAPI(
        title="TADOSS",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
    )
    app.state.pool = pool
    app.state.settings = settings
    app.state.authentication = authentication
    app.add_middleware(RequestAdmission, maximum=settings.max_inflight_requests)
    app.add_middleware(AccessLog)
    app.add_middleware(CorrelationIdMiddleware, header_name="X-Request-ID")
    telemetry.instrument(app)

    @app.exception_handler(APIError)
    async def api_error(request: Request, exc: APIError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(exc.body, status_code=exc.status, headers=headers)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            APIError(exc.status_code, exc.detail).body,
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(psycopg.OperationalError)
    async def database_unavailable(
        request: Request, exc: psycopg.OperationalError
    ) -> JSONResponse:
        logger.warning(
            "Transient database operation failure",
            extra={"exception_type": type(exc).__name__, "sqlstate": exc.sqlstate},
        )
        return JSONResponse(
            APIError(503, "Database operation temporarily failed; retry later").body,
            status_code=503,
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(psycopg.IntegrityError)
    async def conflicting_change(
        request: Request, exc: psycopg.IntegrityError
    ) -> JSONResponse:
        return JSONResponse(
            APIError(409, "A concurrent change conflicts with this request").body,
            status_code=409,
        )

    @app.exception_handler(psycopg.Error)
    async def database_error(request: Request, exc: psycopg.Error) -> JSONResponse:
        logger.error(
            "Database error",
            extra={"exception_type": type(exc).__name__, "sqlstate": exc.sqlstate},
        )
        return JSONResponse(
            APIError(500, "Internal database error").body, status_code=500
        )

    @app.exception_handler(Exception)
    async def server_error(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            APIError(500, "Internal server error").body,
            status_code=500,
            headers={"X-Request-ID": correlation_id.get() or ""},
        )

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics(request: Request) -> Response:
        def collect() -> Response:
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            if not (
                settings.metrics_token
                and scheme.lower() == "bearer"
                and hmac.compare_digest(token.encode(), settings.metrics_token.encode())
            ):
                authentication.authenticate(request)
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

        slots = app.state.metrics_slots
        try:
            slots.acquire_nowait()
        except anyio.WouldBlock:
            return Response(status_code=503, headers={"Retry-After": "1"})
        try:
            return await anyio.to_thread.run_sync(
                collect, limiter=app.state.metrics_threads
            )
        finally:
            slots.release()

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    def check_readiness() -> dict[str, str] | JSONResponse:
        try:
            with store.transaction(probe_pool, timeout=2) as db:
                store.check_schema(db)
        except psycopg.OperationalError:
            return JSONResponse(
                {"status": "not ready", "reason": "database"}, status_code=503
            )
        except RuntimeError:
            return JSONResponse(
                {"status": "not ready", "reason": "schema"}, status_code=503
            )
        return {"status": "ready"}

    @app.get("/readyz", include_in_schema=False, response_model=None)
    async def ready() -> dict[str, str] | JSONResponse:
        # Bound probes separately so auth/route threads cannot starve readiness.
        slots: anyio.CapacityLimiter = app.state.readiness_slots
        try:
            slots.acquire_nowait()
        except anyio.WouldBlock:
            return JSONResponse(
                {"status": "not ready", "reason": "busy"}, status_code=503
            )
        try:
            return await anyio.to_thread.run_sync(
                check_readiness, limiter=app.state.readiness_threads
            )
        finally:
            slots.release()

    app.include_router(router, include_in_schema=False)
    app.openapi = published_contract
    return app
