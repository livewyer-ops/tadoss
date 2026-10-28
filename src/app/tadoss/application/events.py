"""Event validation and persistence, including transactional notifications."""

from typing import Any, cast
from uuid import uuid4

from mediatimestamp.immutable import TimeRange

from tadoss.adapters import postgres as store
from tadoss.application import background
from tadoss.domain.types import Document
from tadoss.domain.validation import (
    APIError,
    check_grid,
    contract_schema,
    now,
    payload_validator,
    storage_safe,
    time_range,
    validate,
    validate_payload,
    writable,
)


def write_events(db: store.Database, track: Document, body: Any) -> Document:
    writable(track)
    batch: list[Any] = cast(list[Any], body) if isinstance(body, list) else [body]
    if (
        not batch
        or len(batch) > 1000
        or any(not isinstance(record, dict) for record in batch)
    ):
        raise APIError(
            400, "Supply an event object or an array of 1 to 1000 event objects"
        )
    validator = payload_validator(
        store.get(db, "schemas", track["schema_id"])["schema"]
    )
    prepared: list[Document | APIError] = []
    for incoming in batch:
        try:
            validate(incoming, contract_schema("track-event-post"))
            storage_safe(incoming)
            prepared.append({**incoming, "id": incoming.get("id", str(uuid4()))})
        except APIError as exc:
            prepared.append(exc)
    identifiers = [record["id"] for record in prepared if isinstance(record, dict)]
    owners = dict(
        db.execute(
            "SELECT id,track_id FROM events WHERE id=ANY(%s)",
            (identifiers,),
        ).fetchall()
    )
    # Duplicate IDs and overlap checks depend on earlier writes in the batch.
    bulk = track["allow_overlaps"] and len(identifiers) == len(set(identifiers))
    pending: list[tuple[Document, TimeRange]] = []
    responses: list[Document] = []
    for record in prepared:
        result: Document = {}
        try:
            if isinstance(record, APIError):
                raise record
            result["id"] = record["id"]
            existing = owners.get(record["id"])
            if existing and existing != track["id"]:
                raise APIError(400, "Event ID already belongs to another track")
            if existing and not track["editable_events"]:
                raise APIError(403, "Track does not permit editing existing events")
            timerange = time_range(record["timerange"], require_non_empty=True)
            check_grid(timerange, track)
            validate_payload(validator, record["payload"])
            if not track["allow_overlaps"] and store.overlapping_exists(
                db, track["id"], timerange, record["id"]
            ):
                raise APIError(400, "Event overlaps an existing event")
            record["timerange"] = str(timerange)
            if bulk:
                pending.append((record, timerange))
            elif record["id"] not in store.upsert_events(
                db, track["id"], [(record, timerange)]
            ):
                raise APIError(400, "Event ID already belongs to another track")
            result["http_status_code"] = 200 if existing else 201
            owners[record["id"]] = track["id"]
        except APIError as exc:
            result.update(http_status_code=exc.status, error=exc.body)
        responses.append(result)
    if pending:
        saved = store.upsert_events(db, track["id"], pending)
        for result in responses:
            if result["http_status_code"] < 300 and result["id"] not in saved:
                error = APIError(400, "Event ID already belongs to another track")
                result.update(http_status_code=error.status, error=error.body)
    if any(result["http_status_code"] < 300 for result in responses):
        track["events_updated"] = now()
        store.save(db, "tracks", track)
    for status, event_type in (
        (201, "tracks/events_added"),
        (200, "tracks/events_updated"),
    ):
        identifiers = [
            row["id"] for row in responses if row["http_status_code"] == status
        ]
        if identifiers:
            background.publish(
                db,
                event_type,
                track,
                {
                    "track_id": track["id"],
                    "track_events": store.event_documents(db, identifiers),
                },
            )
    return {
        "all_successful": all(r["http_status_code"] < 300 for r in responses),
        "responses": responses,
    }
