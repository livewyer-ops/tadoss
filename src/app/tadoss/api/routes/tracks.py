from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from tadoss.adapters import postgres as store
from tadoss.api.dependencies import Body, Database, Query
from tadoss.api.http import (
    collection_page,
    deletion_response,
    property_body,
    property_value,
    read_response,
)
from tadoss.application import background, records

router = APIRouter()


@router.head("/tracks")
@router.get("/tracks")
def list_tracks(request: Request, db: Database, options: Query) -> Response:
    return collection_page(request, db, "tracks", options)


@router.head("/tracks/{track_id}")
@router.get("/tracks/{track_id}")
def get_track(
    request: Request, track_id: str, db: Database, options: Query
) -> Response:
    document = store.get(db, "tracks", track_id)
    if options.get("include_timerange"):
        document["timerange"] = store.track_coverage(
            db, track_id, options.get("timerange")
        )
    return read_response(request, document)


@router.put("/tracks/{track_id}")
def put_track(request: Request, track_id: str, db: Database, body: Body) -> Response:
    created = records.replace(
        db, "tracks", track_id, body, request.state.identity.subject
    )
    if created:
        return JSONResponse(
            body, status_code=201, headers={"Location": str(request.url.path)}
        )
    return Response(status_code=204)


@router.delete("/tracks/{track_id}")
def delete_track(request: Request, track_id: str, db: Database) -> Response:
    track = store.get(db, "tracks", track_id)
    return deletion_response(
        background.request_delete(
            db, track, {}, True, request.state.identity, request.app.state.settings
        )
    )


@router.head("/tracks/{track_id}/tags")
@router.get("/tracks/{track_id}/tags")
@router.head("/tracks/{track_id}/tags/{name}")
@router.get("/tracks/{track_id}/tags/{name}")
@router.head("/tracks/{track_id}/description")
@router.get("/tracks/{track_id}/description")
@router.head("/tracks/{track_id}/label")
@router.get("/tracks/{track_id}/label")
@router.head("/tracks/{track_id}/read_only")
@router.get("/tracks/{track_id}/read_only")
@router.head("/tracks/{track_id}/allow_overlaps")
@router.get("/tracks/{track_id}/allow_overlaps")
@router.head("/tracks/{track_id}/editable_events")
@router.get("/tracks/{track_id}/editable_events")
def get_property(request: Request, track_id: str, db: Database) -> Response:
    return read_response(
        request, property_value(request, store.get(db, "tracks", track_id))
    )


@router.put("/tracks/{track_id}/tags/{name}")
@router.put("/tracks/{track_id}/description")
@router.put("/tracks/{track_id}/label")
@router.put("/tracks/{track_id}/read_only")
@router.put("/tracks/{track_id}/allow_overlaps")
@router.put("/tracks/{track_id}/editable_events")
def put_property(request: Request, track_id: str, db: Database, body: Body) -> Response:
    field = request.scope["route"].path.split("/")[3]
    property_body(request, body)
    records.change_property(
        db,
        "tracks",
        track_id,
        field,
        body,
        request.state.identity.subject,
        tag=request.path_params.get("name"),
        remove=False,
    )
    return Response(status_code=204)


@router.delete("/tracks/{track_id}/tags/{name}")
@router.delete("/tracks/{track_id}/description")
@router.delete("/tracks/{track_id}/label")
def delete_property(request: Request, track_id: str, db: Database) -> Response:
    field = request.scope["route"].path.split("/")[3]
    records.change_property(
        db,
        "tracks",
        track_id,
        field,
        None,
        request.state.identity.subject,
        tag=request.path_params.get("name"),
        remove=True,
    )
    return Response(status_code=204)
