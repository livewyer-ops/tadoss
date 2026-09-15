import base64
import binascii
import copy
import hmac
import json
import logging
import math
import os
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from psycopg import sql
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from . import __version__, store
from .validation import (
    SPEC,
    APIError,
    contract_schema,
    coverage,
    now,
    time_range,
    validate,
)

MAX_BODY = 1024 * 1024
logger = logging.getLogger("tadoss")


def env_flag(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON numbers must be finite")
    return number


def resolve(value):
    if "$ref" in value:
        result = SPEC
        for part in value["$ref"].removeprefix("#/").split("/"):
            result = result[part.replace("~1", "/").replace("~0", "~")]
        return result
    return value


def query_options(request, path, method):
    options = {}
    definitions = [
        resolve(item) for item in SPEC["paths"][path][method].get("parameters", [])
    ]
    for key, raw in request.query_params.multi_items():
        if key == "access_token":
            continue
        if key in options:
            raise APIError(400, f"Duplicate query option: {key}")
        definition = next(
            (
                item
                for item in definitions
                if item["name"] == key
                or (
                    item["name"].endswith(".{name}")
                    and key.startswith(item["name"][:-6])
                    and key != item["name"][:-6]
                )
            ),
            None,
        )
        if definition is None:
            raise APIError(400, f"Unknown query option: {key}")
        schema = resolve(definition["schema"])
        value = raw
        if schema.get("type") == "boolean":
            if raw not in ("true", "false"):
                raise APIError(400, f"{key} must be true or false")
            value = raw == "true"
        elif schema.get("type") == "integer":
            try:
                value = int(raw)
            except ValueError as exc:
                raise APIError(400, f"{key} must be an integer") from exc
        validate(value, definition["schema"])
        options[key] = value
    if "limit" in options and options["limit"] < 1:
        raise APIError(400, "limit must be positive")
    if "timerange" in options:
        options["timerange"] = time_range(options["timerange"])
    if "schema_id" in options:
        validate(options["schema_id"], contract_schema("uuid"))
    if "ids" in options:
        options["ids"] = options["ids"].split(",")
        for identifier in options["ids"]:
            validate(identifier, contract_schema("uuid"))
    return options


def filter_records(records, options):
    result = []
    for document in records:
        matches = True
        for key, value in options.items():
            if key.startswith("tag_exists."):
                matches &= (key[11:] in document.get("tags", {})) == value
            elif key.startswith("tag."):
                tag = document.get("tags", {}).get(key[4:], [])
                matches &= bool(
                    set([tag] if isinstance(tag, str) else tag) & set(value.split(","))
                )
            elif key == "timeline_reference.entity_id":
                matches &= document["timeline_reference"]["entity_id"] in [
                    unquote(uri) for uri in value.split(",")
                ]
            elif key in {"label", "external_id", "schema_id", "editable_events"}:
                matches &= document.get(key) == value
        if matches:
            result.append(document)
    return result


def next_link(request, cursor):
    # Relative links avoid trusting a caller-supplied Host header, and the URL
    # token is never reflected back to the client.
    url = (
        request.url.remove_query_params("access_token")
        .replace(scheme="", netloc="")
        .include_query_params(page=cursor)
    )
    return {"X-Paging-NextKey": cursor, "Link": f'<{url}>; rel="next"'}


def encode_cursor(key, reverse):
    payload = {
        "v": 2,
        "r": reverse,
        "k": [str(v) if isinstance(v, Decimal) else v for v in key],
    }
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()


def decode_cursor(page, reverse):
    if page is None:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(page))
        if payload["v"] != 2 or payload["r"] is not reverse:
            raise ValueError("cursor direction")
        first, first_flag, second, second_flag, identifier = payload["k"]
        key = (Decimal(first), first_flag, Decimal(second), second_flag, identifier)
        if (
            not (
                isinstance(first_flag, bool)
                and isinstance(second_flag, bool)
                and isinstance(identifier, str)
                and isinstance(first, str)
                and isinstance(second, str)
            )
            or key[0].is_nan()
            or key[2].is_nan()
        ):
            raise ValueError("cursor shape")
        return key
    except (ValueError, TypeError, KeyError, InvalidOperation, binascii.Error) as exc:
        raise APIError(400, "Invalid page cursor") from exc


def event_page(request, db, track_id, options):
    limit = min(options.get("limit", 100), 1000)
    reverse = options.get("reverse_order", False)
    rows = store.events(
        db,
        track_id,
        options,
        limit + 1,
        decode_cursor(options.get("page"), reverse),
        reverse,
    )
    page = [document for document, _ in rows[:limit]]
    headers = {
        "X-Paging-Limit": str(limit),
        "X-Paging-Count": str(len(page)),
        "X-Paging-Reverse-Order": str(reverse).lower(),
        "X-Paging-Timerange": coverage(page),
    }
    if len(rows) > limit:
        headers.update(next_link(request, encode_cursor(rows[limit - 1][1], reverse)))
    return JSONResponse(page, headers=headers)


def page_response(request, records, options):
    limit = min(options.get("limit", 100), 1000)
    offset = 0
    if "page" in options:
        try:
            offset = int(
                base64.b64decode(options["page"], altchars=b"-_", validate=True)
            )
        except (ValueError, UnicodeError) as exc:
            raise APIError(400, "Invalid page cursor") from exc
        if offset < 0:
            raise APIError(400, "Invalid page cursor")
    page = records[offset : offset + limit]
    headers = {"X-Paging-Limit": str(limit), "X-Paging-Count": str(len(page))}
    if offset + limit < len(records):
        cursor = base64.urlsafe_b64encode(str(offset + limit).encode()).decode()
        headers.update(next_link(request, cursor))
    return JSONResponse(page, headers=headers)


def dispatch(request, pool, body):
    path = request.scope["route"].path
    method = "get" if request.method == "HEAD" else request.method.lower()
    options = query_options(request, path, method)
    for name, value in request.path_params.items():
        if name != "name":
            validate(value, contract_schema("uuid"))
    if path == "/":
        return JSONResponse(["schemas", "service", "tracks", "track-delete-requests"])
    if path == "/service":
        return JSONResponse(
            {
                "name": "TADOSS",
                "type": "urn:x-tads:service:tadoss",
                "api_version": "0.1",
                "service_version": __version__,
            }
        )
    if path == "/track-delete-requests":
        return page_response(request, [], options)
    if path.startswith("/track-delete-requests/"):
        raise APIError(
            404, "Deletion request not found; deletions complete synchronously"
        )
    collection = path.split("/")[1]
    identifier = request.path_params.get(
        "track_id", request.path_params.get("schema_id")
    )
    write = method in {"put", "post", "delete"}
    with store.transaction(pool, write=write, lock=identifier) as db:
        if identifier is None:
            records = store.listing(db, collection, options.get("timerange"))
            return page_response(request, filter_records(records, options), options)
        document = store.get(db, collection, identifier, required=False)
        child = path.split("/")[3:]
        if not child and method == "put":
            validate(
                body, contract_schema("track" if collection == "tracks" else "schema")
            )
            if body["id"] != identifier:
                raise APIError(400, "Body id must match the path id")
            save_record = (
                store.save_track if collection == "tracks" else store.save_schema
            )
            save_record(db, body, document)
            return (
                Response(status_code=204)
                if document
                else JSONResponse(
                    body, status_code=201, headers={"Location": str(request.url.path)}
                )
            )
        if document is None:
            raise APIError(404, "Resource not found")
        if child and child[0] == "events":
            if method == "post":
                return JSONResponse(store.write_events(db, document, body))
            if method == "delete":
                store.writable(document)
                if store.delete_events(db, identifier, options):
                    document["events_updated"] = now()
                    store.save(db, "tracks", document)
                return Response(status_code=204)
            return event_page(request, db, identifier, options)
        if not child:
            if method == "get":
                if collection == "tracks" and options.get("include_timerange"):
                    document["timerange"] = store.track_coverage(
                        db, identifier, options.get("timerange")
                    )
                return JSONResponse(document)
            if collection == "tracks":
                store.writable(document)
            elif db.execute(
                "SELECT 1 FROM tracks WHERE schema_id = %s LIMIT 1", (identifier,)
            ).fetchone():
                raise APIError(400, "Payload schema is referenced by a track")
            db.execute(
                sql.SQL("DELETE FROM {} WHERE id = %s").format(
                    sql.Identifier(collection)
                ),
                (identifier,),
            )
            return Response(status_code=204)
        target = document["tags"] if len(child) == 2 else document
        key = request.path_params["name"] if len(child) == 2 else child[0]
        if method == "get":
            if key not in target:
                raise APIError(404, "Property not found")
            return JSONResponse(target[key])
        old = copy.deepcopy(document)
        if method == "delete":
            if key not in target:
                raise APIError(404, "Property not found")
            del target[key]
        else:
            schema = SPEC["paths"][path]["put"]["requestBody"]["content"][
                "application/json"
            ]["schema"]
            validate(body, schema)
            target[key] = body
        if collection == "tracks":
            store.save_track(db, document, old, unlocking=child == ["read_only"])
        else:
            store.save_schema(db, document, old)
        return Response(status_code=204)


def create_app(database=None, token=None, dev=None):
    token = token if token is not None else os.getenv("TADOSS_TOKEN", "")
    dev = dev if dev is not None else env_flag("TADOSS_DEV", False)
    database = (
        database
        or os.getenv("TADOSS_DATABASE")
        or (store.DEFAULT_DATABASE if dev else None)
    )
    if database is None:
        raise RuntimeError("Set TADOSS_DATABASE")
    if not token and not dev:
        raise RuntimeError("Set TADOSS_TOKEN, or TADOSS_DEV=true for local development")
    if not token:
        logger.warning("Authentication disabled: development mode without a token")
    auto_migrate = env_flag("TADOSS_AUTO_MIGRATE", True)
    pool = store.create_pool(
        database,
        min_size=int(os.getenv("TADOSS_POOL_MIN_SIZE", "1")),
        max_size=int(os.getenv("TADOSS_POOL_MAX_SIZE", "10")),
        timeout=float(os.getenv("TADOSS_POOL_TIMEOUT", "30")),
    )

    @asynccontextmanager
    async def lifespan(app):
        # Connections are established in the background; a request waits up to the
        # pool timeout for one, so a slow database delays readiness instead of
        # crashing the process.
        pool.open(wait=False)
        try:
            if auto_migrate:
                await run_in_threadpool(store.initialize, pool)
            yield
        finally:
            pool.close()

    app = FastAPI(title="TADOSS", version=__version__, lifespan=lifespan)
    app.state.pool = pool

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
        return JSONResponse(
            APIError(503, "Database unavailable; retry later").body,
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
        logger.exception("Database error", exc_info=exc)
        return JSONResponse(
            APIError(500, "Internal database error").body, status_code=500
        )

    @app.exception_handler(Exception)
    async def server_error(request, exc):
        return JSONResponse(
            APIError(500, "Internal server error").body, status_code=500
        )

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
        if token:
            authorization = request.headers.get("authorization", "")
            scheme, _, credential = authorization.partition(" ")
            candidate = (
                credential
                if scheme.lower() == "bearer"
                else request.query_params.get("access_token", "")
            )
            if not hmac.compare_digest(candidate.encode(), token.encode()):
                raise APIError(401, "A valid bearer token is required")
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

    contract = copy.deepcopy(SPEC)
    contract["info"] = {
        **contract["info"],
        "title": "TADOSS",
        "description": (
            "TADS 0.1 core API. Webhook delivery is not implemented. "
            "Deletions complete synchronously."
        ),
    }
    contract["info"].pop("contact", None)
    contract["servers"] = [{"url": "/"}]
    contract["security"] = [{"bearer_token_auth": []}, {"url_token_auth": []}]
    contract["components"]["securitySchemes"].pop("basic_auth", None)
    contract.pop("webhooks", None)
    for path in list(contract["paths"]):
        if path.startswith("/service/webhooks"):
            del contract["paths"][path]
            continue
        methods = [
            method.upper()
            for method in contract["paths"][path]
            if method != "parameters"
        ]
        for operation in contract["paths"][path].values():
            if isinstance(operation, dict):
                operation.get("responses", {}).pop("202", None)
        app.add_api_route(path, endpoint, methods=methods, include_in_schema=False)
    app.openapi = lambda: contract
    return app
