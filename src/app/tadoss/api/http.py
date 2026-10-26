import base64
import binascii
import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from tadoss.adapters import postgres as store
from tadoss.domain.types import Document, EventCursor, Options
from tadoss.domain.validation import (
    SPEC,
    APIError,
    contract_schema,
    coverage,
    storage_safe,
    time_range,
    validate,
)


def finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON numbers must be finite")
    return number


def resolve(value: Document) -> Document:
    if "$ref" in value:
        result = SPEC
        for part in value["$ref"].removeprefix("#/").split("/"):
            result = result[part.replace("~1", "/").replace("~0", "~")]
        return result
    return value


def query_options(request: Request, path: str, method: str) -> Options:
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
        storage_safe(value)
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


def next_link(request: Request, cursor: str) -> dict[str, str]:
    # Relative links avoid trusting a caller-supplied Host header, and the URL
    # token is never reflected back to the client.
    url = (
        request.url.remove_query_params("access_token")
        .replace(scheme="", netloc="")
        .include_query_params(page=cursor)
    )
    return {"X-Paging-NextKey": cursor, "Link": f'<{url}>; rel="next"'}


def encode_cursor(key: EventCursor, reverse: bool) -> str:
    payload = {
        "v": 2,
        "r": reverse,
        "k": [str(v) if isinstance(v, Decimal) else v for v in key],
    }
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()


def decode_cursor(page: str | None, reverse: bool) -> EventCursor | None:
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
        validate(identifier, contract_schema("uuid"))
        return key
    except (ValueError, TypeError, KeyError, InvalidOperation, binascii.Error) as exc:
        raise APIError(400, "Invalid page cursor") from exc


def event_page(
    request: Request, db: store.Database, track_id: str, options: Options
) -> Response:
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
    return read_response(request, page, headers=headers)


def paged(
    request: Request, records: list[Document], options: Options, deletion: bool = False
) -> Response:
    limit = min(options.get("limit", 100), 1000)
    page = records[:limit]
    headers = {"X-Paging-Limit": str(limit), "X-Paging-Count": str(len(page))}
    if deletion:
        headers["X-Paging-Reverse-Order"] = str(
            options.get("reverse_order", False)
        ).lower()
    if len(records) > limit:
        key = (
            [
                options.get("sort_by", "created"),
                options.get("reverse_order", False),
                page[-1].get(options.get("sort_by", "created")),
                page[-1]["id"],
            ]
            if deletion
            else page[-1]["id"]
        )
        cursor = base64.urlsafe_b64encode(json.dumps(key).encode()).decode()
        headers.update(next_link(request, cursor))
    return read_response(request, page, headers=headers)


def read_response(
    request: Request, value: Any, *, headers: dict[str, str] | None = None
) -> Response:
    response = JSONResponse(value, headers=headers)
    if request.method == "HEAD":
        response.body = b""
    return response


def collection_page(
    request: Request, db: store.Database, table: str, options: Options
) -> Response:
    limit = min(options.get("limit", 100), 1000)
    after = None
    if cursor := options.get("page"):
        try:
            after = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            validate(after, contract_schema("uuid"))
        except (ValueError, UnicodeError) as exc:
            raise APIError(400, "Invalid page cursor") from exc
    return paged(request, store.listing(db, table, options, limit + 1, after), options)


def property_value(request: Request, document: Document) -> Any:
    field = request.scope["route"].path_format.split("/")[3]
    tag = request.path_params.get("name")
    target, key = (document, field) if tag is None else (document["tags"], tag)
    if key not in target:
        raise APIError(404, "Property not found")
    return target[key]


def validate_body(request: Request, body: Any) -> None:
    operation = SPEC["paths"][request.scope["route"].path_format][
        request.method.lower()
    ]
    validate(body, operation["requestBody"]["content"]["application/json"]["schema"])


def deletion_response(document: Document | None) -> Response:
    if document is None:
        return Response(status_code=204)
    return JSONResponse(
        document,
        status_code=202,
        headers={"Location": "/track-delete-requests/" + document["id"]},
    )
