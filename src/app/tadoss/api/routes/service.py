from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from tadoss import __version__
from tadoss.adapters import postgres as store
from tadoss.api.dependencies import Body, Database, Query
from tadoss.api.http import collection_page, paged, read_response
from tadoss.application import background

router = APIRouter()


@router.head("/")
@router.get("/")
def root(request: Request, _options: Query) -> Response:
    return read_response(
        request, ["schemas", "service", "tracks", "track-delete-requests"]
    )


@router.head("/service")
@router.get("/service")
def service(request: Request, _options: Query) -> Response:
    return read_response(
        request,
        {
            "name": "TADOSS",
            "type": "urn:x-tads:service:tadoss",
            "api_version": "0.1",
            "service_version": __version__,
        },
    )


@router.head("/track-delete-requests")
@router.get("/track-delete-requests")
def list_deletions(request: Request, db: Database, options: Query) -> Response:
    return paged(request, background.list_deletes(db, options), options, deletion=True)


@router.head("/track-delete-requests/{request_id}")
@router.get("/track-delete-requests/{request_id}")
def get_deletion(request: Request, request_id: str, db: Database) -> Response:
    return read_response(request, background.get_delete(db, request_id))


@router.head("/service/webhooks")
@router.get("/service/webhooks")
def list_webhooks(request: Request, db: Database, options: Query) -> Response:
    identity = request.state.identity
    if not identity.admin:
        options["_owner"] = identity.subject
    return collection_page(request, db, "webhooks", options)


@router.post("/service/webhooks")
def create_webhook(request: Request, db: Database, body: Body) -> Response:
    identifier = str(uuid4())
    document = background.webhook_save(
        db, identifier, body, request.state.identity, request.app.state.settings
    )
    return JSONResponse(
        document,
        status_code=201,
        headers={"Location": "/service/webhooks/" + identifier},
    )


@router.head("/service/webhooks/{webhook_id}")
@router.get("/service/webhooks/{webhook_id}")
def get_webhook(request: Request, webhook_id: str, db: Database) -> Response:
    return read_response(
        request, background.webhook_get(db, webhook_id, request.state.identity)
    )


@router.put("/service/webhooks/{webhook_id}")
def replace_webhook(
    request: Request, webhook_id: str, db: Database, body: Body
) -> Response:
    document = background.webhook_save(
        db,
        webhook_id,
        body,
        request.state.identity,
        request.app.state.settings,
        replacing=True,
    )
    return JSONResponse(document)


@router.delete("/service/webhooks/{webhook_id}")
def delete_webhook(request: Request, webhook_id: str, db: Database) -> Response:
    background.webhook_get(db, webhook_id, request.state.identity)
    store.delete(db, "webhooks", webhook_id)
    return Response(status_code=204)
