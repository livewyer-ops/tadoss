import base64
import copy
import hmac
import json
import logging
import math
import os
from contextlib import asynccontextmanager
from urllib.parse import unquote

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from . import __version__, store
from .validation import (
    SPEC,
    APIError,
    contract_schema,
    coverage,
    event_key,
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


def filter_events(events, options, deleting=False):
    timerange = options.get("timerange")
    return [
        event
        for event in events
        if ("ids" not in options or event["id"] in options["ids"])
        and (
            timerange is None
            or (
                timerange.contains_subrange(time_range(event["timerange"]))
                if deleting
                else timerange.overlaps_with_timerange(time_range(event["timerange"]))
            )
        )
    ]


def page_response(request, records, options, event_listing=False):
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
        # Relative links avoid trusting a caller-supplied Host header, and the
        # URL token is never reflected back to the client.
        url = (
            request.url.remove_query_params("access_token")
            .replace(scheme="", netloc="")
            .include_query_params(page=cursor)
        )
        headers.update({"X-Paging-NextKey": cursor, "Link": f'<{url}>; rel="next"'})
    if event_listing:
        headers["X-Paging-Reverse-Order"] = str(
            options.get("reverse_order", False)
        ).lower()
        headers["X-Paging-Timerange"] = coverage(page)
    return JSONResponse(page, headers=headers)


def dispatch(request, database, body):
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
    with store.transaction(database, write=method in {"put", "post", "delete"}) as db:
        collection = path.split("/")[1]
        identifier = request.path_params.get(
            "track_id", request.path_params.get("schema_id")
        )
        if identifier is None:
            records = filter_records(store.listing(db, collection), options)
            if "timerange" in options:
                records = [
                    record
                    for record in records
                    if filter_events(store.events(db, record["id"]), options)
                ]
            return page_response(request, records, options)
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
            selected = filter_events(
                store.events(db, identifier), options, deleting=method == "delete"
            )
            if method == "delete":
                store.writable(document)
                if store.delete_events(
                    db, identifier, [event["id"] for event in selected]
                ):
                    document["events_updated"] = now()
                    store.save(db, "tracks", document)
                return Response(status_code=204)
            reverse = options.get("reverse_order", False)
            selected.sort(key=lambda event: event_key(event, reverse), reverse=reverse)
            return page_response(request, selected, options, event_listing=True)
        if not child:
            if method == "get":
                if collection == "tracks" and options.get("include_timerange"):
                    document["timerange"] = coverage(
                        filter_events(store.events(db, identifier), options)
                    )
                return JSONResponse(document)
            if collection == "tracks":
                store.writable(document)
            elif db.execute(
                "SELECT 1 FROM tracks WHERE schema_id = %s LIMIT 1", (identifier,)
            ).fetchone():
                raise APIError(400, "Payload schema is referenced by a track")
            db.execute(f"DELETE FROM {collection} WHERE id = %s", (identifier,))
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

    @asynccontextmanager
    async def lifespan(app):
        if auto_migrate:
            await run_in_threadpool(store.initialize, database)
        yield

    app = FastAPI(title="TADOSS", version=__version__, lifespan=lifespan)

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
        with store.transaction(database) as db:
            store.check_schema(db)
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
        response = await run_in_threadpool(dispatch, request, database, body)
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
