"""FastAPI dependencies for validated requests and transaction lifetimes."""

import json
from collections.abc import AsyncIterator, Iterator
from typing import Annotated, Any

import anyio
from fastapi import Depends, Request
from psycopg_pool import PoolTimeout

from tadoss.adapters import postgres as store
from tadoss.application import background
from tadoss.auth import Identity
from tadoss.domain.types import Options
from tadoss.domain.validation import APIError, contract_schema, validate

from .http import finite_float, query_options

MAX_BODY = 1024 * 1024


def authenticate(request: Request) -> Identity:
    identity = request.app.state.authentication.authenticate(request)
    request.state.identity = identity
    return identity


async def json_body(
    request: Request, _identity: Annotated[Identity, Depends(authenticate)]
) -> Any:
    if request.method not in {"PUT", "POST"}:
        return None
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
        return json.loads(raw, parse_constant=finite_float, parse_float=finite_float)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise APIError(400, "Request body must be valid JSON") from exc


def options(request: Request, _body: Annotated[Any, Depends(json_body)]) -> Options:
    path = request.scope["route"].path
    method = "get" if request.method == "HEAD" else request.method.lower()
    result = query_options(request, path, method)
    for name, value in request.path_params.items():
        if name != "name":
            validate(value, contract_schema("uuid"))
    return result


async def database_slot(
    request: Request, _options: Annotated[Options, Depends(options)]
) -> AsyncIterator[None]:
    # Wait without consuming the threads needed by requests holding connections.
    slots: anyio.CapacityLimiter = request.app.state.database_slots
    try:
        with anyio.fail_after(request.app.state.settings.pool_timeout):
            await slots.acquire()
    except TimeoutError:
        raise PoolTimeout("Database request admission timed out") from None
    try:
        # Synchronous work and transaction cleanup must finish before cancellation.
        with anyio.CancelScope(shield=True):
            yield
    finally:
        slots.release()


def database(
    request: Request,
    _slot: Annotated[None, Depends(database_slot, scope="function")],
) -> Iterator[store.Database]:
    parameters = request.path_params
    identifier = parameters.get("track_id", parameters.get("schema_id"))
    write = request.method in {"PUT", "POST", "DELETE"}
    with store.transaction(
        request.app.state.pool,
        write=write,
        lock=identifier or parameters.get("webhook_id"),
    ) as db:
        try:
            store.check_schema(db)
        except RuntimeError:
            raise APIError(503, "Database schema is not ready") from None
        if write and "track_id" in parameters:
            background.ensure_track_available(db, parameters["track_id"])
        yield db


# Commit before sending the response, including failures raised by commit itself.
Database = Annotated[store.Database, Depends(database, scope="function")]
Body = Annotated[Any, Depends(json_body)]
Query = Annotated[Options, Depends(options)]
