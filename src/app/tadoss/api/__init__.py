import copy
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from asgi_correlation_id import CorrelationIdMiddleware, correlation_id
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from tadoss import __version__, metrics, telemetry
from tadoss.adapters import postgres as store
from tadoss.auth import Authentication
from tadoss.domain.validation import (
    SPEC,
    APIError,
)
from tadoss.settings import Settings

from .http import finite_float
from .routes import dispatch

MAX_BODY = 1024 * 1024
PROBES = {"/healthz", "/readyz"}
logger = logging.getLogger("tadoss")
access_logger = logging.getLogger("tadoss.access")


class AccessLog:
    """Logs method, route template, status and duration; never the query string."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def record(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, record)
        finally:
            route = scope.get("route")
            template = route.path if route is not None else scope["path"]
            client = scope.get("client")
            metric_route = route.path if route is not None else "unmatched"
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


def published_contract():
    contract = copy.deepcopy(SPEC)
    contract["info"]["title"] = "TADOSS"
    contract["info"].pop("contact", None)
    contract["servers"] = [{"url": "/"}]
    return contract


def create_app(database=None, token=None, dev=None, settings=None):
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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Connections are established in the background; a request waits up to the
        # pool timeout for one, so a slow database delays readiness instead of
        # crashing the process.
        pool.open(wait=False)
        try:
            yield
        finally:
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
    app.add_middleware(AccessLog)
    app.add_middleware(CorrelationIdMiddleware, header_name="X-Request-ID")
    telemetry.instrument(app)

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(exc.body, status_code=exc.status, headers=headers)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(
            APIError(exc.status_code, exc.detail).body,
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(psycopg.OperationalError)
    async def database_unavailable(request, exc):
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
    async def conflicting_change(request, exc):
        return JSONResponse(
            APIError(409, "A concurrent change conflicts with this request").body,
            status_code=409,
        )

    @app.exception_handler(psycopg.Error)
    async def database_error(request, exc):
        logger.error(
            "Database error",
            extra={"exception_type": type(exc).__name__, "sqlstate": exc.sqlstate},
        )
        return JSONResponse(
            APIError(500, "Internal database error").body, status_code=500
        )

    @app.exception_handler(Exception)
    async def server_error(request, exc):
        return JSONResponse(
            APIError(500, "Internal server error").body,
            status_code=500,
            headers={"X-Request-ID": correlation_id.get() or ""},
        )

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics(request: Request):
        authentication.authenticate(request)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/healthz", include_in_schema=False)
    def health():
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def ready():
        try:
            with store.transaction(pool, timeout=2) as db:
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

    async def endpoint(request: Request):
        request.state.identity = await run_in_threadpool(
            authentication.authenticate, request
        )
        body = None
        if request.method in {"PUT", "POST"}:
            if (
                request.headers.get("content-type", "").split(";")[0].lower()
                != "application/json"
            ):
                raise APIError(415, "Content-Type must be application/json")
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > MAX_BODY:
                    raise APIError(413, "Request body exceeds 1 MiB")
                raw.extend(chunk)
            try:
                body = json.loads(
                    raw,
                    parse_constant=finite_float,
                    parse_float=finite_float,
                )
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise APIError(400, "Request body must be valid JSON") from exc
        response = await run_in_threadpool(dispatch, request, pool, body)
        if request.method == "HEAD":
            response.body = b""
        return response

    contract = published_contract()
    for path, operations in contract["paths"].items():
        methods = [m.upper() for m in operations if m != "parameters"]
        app.add_api_route(path, endpoint, methods=methods, include_in_schema=False)
    app.openapi = lambda: contract
    return app
