from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from tadoss import metrics
from tadoss.adapters import postgres as store
from tadoss.api.dependencies import Body, Database, Query
from tadoss.api.http import deletion_response, event_page, validate_body
from tadoss.application import background, events

router = APIRouter()


@router.head("/tracks/{track_id}/events")
@router.get("/tracks/{track_id}/events")
def list_events(
    request: Request, track_id: str, db: Database, options: Query
) -> Response:
    store.get(db, "tracks", track_id)
    return event_page(request, db, track_id, options)


@router.post("/tracks/{track_id}/events")
def write_events(request: Request, track_id: str, db: Database, body: Body) -> Response:
    validate_body(request, body)
    metrics.BATCH.observe(len(body) if isinstance(body, list) else 1)
    result = events.write_events(db, store.get(db, "tracks", track_id), body)
    request.state.event_results = [
        row["http_status_code"] for row in result["responses"]
    ]
    return JSONResponse(result)


@router.delete("/tracks/{track_id}/events")
def delete_events(
    request: Request, track_id: str, db: Database, options: Query
) -> Response:
    track = store.get(db, "tracks", track_id)
    return deletion_response(
        background.request_delete(
            db,
            track,
            options,
            False,
            request.state.identity,
            request.app.state.settings,
        )
    )
