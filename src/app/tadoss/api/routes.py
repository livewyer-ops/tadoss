import base64
import copy
import json
from dataclasses import dataclass
from functools import partial
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from tadoss import __version__
from tadoss.adapters import postgres as store
from tadoss.application import background
from tadoss.domain.validation import (
    SPEC,
    APIError,
    contract_schema,
    validate,
)

from .http import event_page, paged, query_options


@dataclass
class Call:
    """One request after authentication, body parsing and option validation."""

    request: Request
    path: str
    method: str
    options: dict
    body: object
    params: dict
    db: object = None

    @property
    def identifier(self):
        return self.params.get("track_id", self.params.get("schema_id"))


def handle_root(call):
    return JSONResponse(["schemas", "service", "tracks", "track-delete-requests"])


def handle_service(call):
    return JSONResponse(
        {
            "name": "TADOSS",
            "type": "urn:x-tads:service:tadoss",
            "api_version": "0.1",
            "service_version": __version__,
        }
    )


def handle_delete_requests(call):
    records = background.list_deletes(call.db, call.options)
    return paged(call.request, records, call.options, deletion=True)


def handle_delete_request(call):
    return JSONResponse(background.get_delete(call.db, call.params["request_id"]))


def handle_webhooks(call):
    identity = call.request.state.identity
    identifier = call.params.get("webhook_id")
    if identifier:
        old = background.webhook_get(call.db, identifier, identity)
        if call.method == "get":
            return JSONResponse(old)
        if call.method == "delete":
            call.db.execute("DELETE FROM webhooks WHERE id=%s", (identifier,))
        else:
            background.webhook_save(
                call.db,
                identifier,
                call.body,
                identity,
                call.request.app.state.settings,
                replacing=True,
            )
        return Response(status_code=204)
    if call.method == "post":
        identifier = str(uuid4())
        document = background.webhook_save(
            call.db, identifier, call.body, identity, call.request.app.state.settings
        )
        return JSONResponse(
            document,
            status_code=201,
            headers={"Location": "/service/webhooks/" + identifier},
        )
    if not identity.admin:
        call.options["_owner"] = identity.subject
    return handle_collection(call, "webhooks")


def handle_collection(call, table):
    limit = min(call.options.get("limit", 100), 1000)
    after = None
    if cursor := call.options.get("page"):
        try:
            after = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            validate(after, contract_schema("uuid"))
        except (ValueError, UnicodeError) as exc:
            raise APIError(400, "Invalid page cursor") from exc
    records = store.listing(call.db, table, call.options, limit + 1, after)
    return paged(call.request, records, call.options)


def handle_resource(call, table):
    identifier = call.identifier
    document = store.find(call.db, table, identifier)
    if call.method == "put":
        validate(call.body, contract_schema("track" if table == "tracks" else "schema"))
        if call.body["id"] != identifier:
            raise APIError(400, "Body id must match the path id")
        save_record = store.save_track if table == "tracks" else store.save_schema
        save_record(
            call.db, call.body, document, subject=call.request.state.identity.subject
        )
        background.publish(
            call.db,
            table + ("/updated" if document else "/created"),
            call.body,
            {"track" if table == "tracks" else "schema": call.body},
        )
        if document:
            return Response(status_code=204)
        location = {"Location": str(call.request.url.path)}
        return JSONResponse(call.body, status_code=201, headers=location)
    if document is None:
        raise APIError(404, "Resource not found")
    if call.method == "get":
        if table == "tracks" and call.options.get("include_timerange"):
            document["timerange"] = store.track_coverage(
                call.db, identifier, call.options.get("timerange")
            )
        return JSONResponse(document)
    if table == "tracks":
        request = background.request_delete(
            call.db,
            document,
            {},
            True,
            call.request.state.identity,
            call.request.app.state.settings,
        )
        return deletion_response(request)
    elif store.schema_in_use(call.db, identifier):
        raise APIError(400, "Payload schema is referenced by a track")
    store.delete(call.db, table, identifier)
    background.publish(call.db, "schemas/deleted", document, {"schema_id": identifier})
    return Response(status_code=204)


def deletion_response(request):
    if request is None:
        return Response(status_code=204)
    return JSONResponse(
        request,
        status_code=202,
        headers={"Location": "/track-delete-requests/" + request["id"]},
    )


def handle_events(call):
    track = store.get(call.db, "tracks", call.identifier)
    if call.method == "post":
        result = store.write_events(call.db, track, call.body)
        for status, event_type in (
            (201, "tracks/events_added"),
            (200, "tracks/events_updated"),
        ):
            identifiers = [
                row["id"]
                for row in result["responses"]
                if row["http_status_code"] == status
            ]
            if identifiers:
                documents = [
                    row[0]
                    for row in call.db.execute(
                        "SELECT document FROM events WHERE id=ANY(%s)", (identifiers,)
                    )
                ]
                background.publish(
                    call.db,
                    event_type,
                    track,
                    {"track_id": track["id"], "track_events": documents},
                )
        return JSONResponse(result)
    if call.method == "delete":
        return deletion_response(
            background.request_delete(
                call.db,
                track,
                call.options,
                False,
                call.request.state.identity,
                call.request.app.state.settings,
            )
        )
    return event_page(call.request, call.db, call.identifier, call.options)


def handle_property(call, table, key=None):
    """Read, replace or remove one property, or one tag when key is None."""
    document = store.get(call.db, table, call.identifier)
    unlocking = key == "read_only"
    target = document if key is not None else document["tags"]
    key = key if key is not None else call.params["name"]
    if call.method == "get":
        if key not in target:
            raise APIError(404, "Property not found")
        return JSONResponse(target[key])
    old = copy.deepcopy(document)
    if call.method == "delete":
        if key not in target:
            raise APIError(404, "Property not found")
        del target[key]
    else:
        operation = SPEC["paths"][call.path]["put"]
        validate(
            call.body, operation["requestBody"]["content"]["application/json"]["schema"]
        )
        target[key] = call.body
    if table == "tracks":
        store.save_track(
            call.db,
            document,
            old,
            unlocking=unlocking,
            subject=call.request.state.identity.subject,
        )
    else:
        store.save_schema(
            call.db, document, old, subject=call.request.state.identity.subject
        )
    background.publish(
        call.db,
        table + "/updated",
        document,
        {"track" if table == "tracks" else "schema": document},
    )
    return Response(status_code=204)


# Contract path -> (handler, needs a database transaction). Kept explicit so the
# published contract and the implemented routes can be compared in tests.
HANDLERS = {
    "/": (handle_root, False),
    "/service": (handle_service, False),
    "/service/webhooks": (handle_webhooks, True),
    "/service/webhooks/{webhook_id}": (handle_webhooks, True),
    "/track-delete-requests": (handle_delete_requests, True),
    "/track-delete-requests/{request_id}": (handle_delete_request, True),
    "/schemas": (partial(handle_collection, table="schemas"), True),
    "/schemas/{schema_id}": (partial(handle_resource, table="schemas"), True),
    "/schemas/{schema_id}/schema": (
        partial(handle_property, table="schemas", key="schema"),
        True,
    ),
    "/schemas/{schema_id}/tags": (
        partial(handle_property, table="schemas", key="tags"),
        True,
    ),
    "/schemas/{schema_id}/tags/{name}": (
        partial(handle_property, table="schemas"),
        True,
    ),
    "/schemas/{schema_id}/description": (
        partial(handle_property, table="schemas", key="description"),
        True,
    ),
    "/schemas/{schema_id}/label": (
        partial(handle_property, table="schemas", key="label"),
        True,
    ),
    "/schemas/{schema_id}/external_id": (
        partial(handle_property, table="schemas", key="external_id"),
        True,
    ),
    "/tracks": (partial(handle_collection, table="tracks"), True),
    "/tracks/{track_id}": (partial(handle_resource, table="tracks"), True),
    "/tracks/{track_id}/tags": (
        partial(handle_property, table="tracks", key="tags"),
        True,
    ),
    "/tracks/{track_id}/tags/{name}": (partial(handle_property, table="tracks"), True),
    "/tracks/{track_id}/description": (
        partial(handle_property, table="tracks", key="description"),
        True,
    ),
    "/tracks/{track_id}/label": (
        partial(handle_property, table="tracks", key="label"),
        True,
    ),
    "/tracks/{track_id}/read_only": (
        partial(handle_property, table="tracks", key="read_only"),
        True,
    ),
    "/tracks/{track_id}/allow_overlaps": (
        partial(handle_property, table="tracks", key="allow_overlaps"),
        True,
    ),
    "/tracks/{track_id}/editable_events": (
        partial(handle_property, table="tracks", key="editable_events"),
        True,
    ),
    "/tracks/{track_id}/events": (handle_events, True),
}


def dispatch(request, pool, body):
    path = request.scope["route"].path
    method = "get" if request.method == "HEAD" else request.method.lower()
    options = query_options(request, path, method)
    for name, value in request.path_params.items():
        if name != "name":
            validate(value, contract_schema("uuid"))
    handler, transactional = HANDLERS[path]
    call = Call(request, path, method, options, body, dict(request.path_params))
    if not transactional:
        return handler(call)
    write = method in {"put", "post", "delete"}
    with store.transaction(
        pool, write=write, lock=call.identifier or call.params.get("webhook_id")
    ) as db:
        call.db = db
        if write and "track_id" in call.params:
            background.ensure_track_available(db, call.identifier)
        return handler(call)
