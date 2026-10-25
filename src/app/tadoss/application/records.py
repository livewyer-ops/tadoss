"""Schema and track mutation rules, within the caller's transaction."""

import copy
from uuid import uuid4

from tadoss.adapters import postgres as store
from tadoss.application import background
from tadoss.domain.types import Document, ResourceTable
from tadoss.domain.validation import (
    APIError,
    check_grid_timestamps,
    contract_schema,
    json_equal,
    now,
    payload_validator,
    storage_safe,
    validate,
    writable,
)


def replace(
    db: store.Database,
    table: ResourceTable,
    identifier: str,
    document: Document,
    subject: str,
) -> bool:
    old = store.find(db, table, identifier)
    validate(document, contract_schema("track" if table == "tracks" else "schema"))
    if document["id"] != identifier:
        raise APIError(400, "Body id must match the path id")
    save_record = save_track if table == "tracks" else save_schema
    save_record(db, document, old, subject=subject)
    background.publish(
        db,
        table + ("/updated" if old else "/created"),
        document,
        {"track" if table == "tracks" else "schema": document},
    )
    return old is None


def change_property(
    db: store.Database,
    table: ResourceTable,
    identifier: str,
    field: str,
    value: object,
    subject: str,
    *,
    tag: str | None = None,
    remove: bool = False,
) -> None:
    document = store.get(db, table, identifier)
    old = copy.deepcopy(document)
    target = document if tag is None else document["tags"]
    key = field if tag is None else tag
    if remove:
        if key not in target:
            raise APIError(404, "Property not found")
        del target[key]
    else:
        target[key] = value
    if table == "tracks":
        save_track(db, document, old, unlocking=field == "read_only", subject=subject)
    else:
        save_schema(db, document, old, subject=subject)
    background.publish(
        db,
        table + "/updated",
        document,
        {"track" if table == "tracks" else "schema": document},
    )


def delete_schema(db: store.Database, identifier: str) -> None:
    document = store.get(db, "schemas", identifier)
    if store.schema_in_use(db, identifier):
        raise APIError(400, "Payload schema is referenced by a track")
    store.delete(db, "schemas", identifier)
    background.publish(db, "schemas/deleted", document, {"schema_id": identifier})


def save_schema(
    db: store.Database,
    document: Document,
    old: Document | None = None,
    subject: str = "tadoss",
) -> None:
    validate(document, contract_schema("schema"))
    storage_safe(document)
    payload_validator(document["schema"])
    if old and not json_equal(old["schema"], document["schema"]):
        raise APIError(403, "A payload schema's schema is immutable")
    timestamp = now()
    document["tags"] = document.get("tags", {})
    document["created"] = old["created"] if old else timestamp
    document["updated"] = timestamp
    document["created_by"] = old["created_by"] if old else subject
    document["updated_by"] = subject
    store.save(db, "schemas", document)


def save_track(
    db: store.Database,
    document: Document,
    old: Document | None = None,
    unlocking: bool = False,
    subject: str = "tadoss",
) -> None:
    validate(document, contract_schema("track"))
    storage_safe(document)
    if old and not unlocking:
        writable(old)
    store.get(db, "schemas", document["schema_id"])
    for key, default in (
        ("tags", dict[str, object]()),
        ("read_only", False),
        ("allow_overlaps", True),
        ("editable_events", True),
    ):
        document.setdefault(key, default)
    document.pop("timerange", None)
    if old:
        if old["schema_id"] != document["schema_id"]:
            raise APIError(400, "A track's schema_id is immutable")
        populated = store.track_has_events(db, old["id"])
        if populated and old["timeline_reference"] != document["timeline_reference"]:
            raise APIError(
                400, "Cannot change the timeline reference of a populated track"
            )
        if (
            populated
            and document.get("edit_rate")
            and document.get("edit_rate") != old.get("edit_rate")
        ):
            check_grid_timestamps(store.track_bounds(db, old["id"]), document)
        if (
            old["allow_overlaps"]
            and not document["allow_overlaps"]
            and store.track_has_overlaps(db, old["id"])
        ):
            raise APIError(400, "Track already contains overlapping events")
    timestamp = now()
    document["created"] = old["created"] if old else timestamp
    document["created_by"] = old["created_by"] if old else subject
    document["updated_by"] = subject
    document["events_updated"] = old["events_updated"] if old else timestamp
    ignored = {"metadata_version", "metadata_updated", "events_updated"}
    if old is None or not json_equal(
        {k: v for k, v in document.items() if k not in ignored},
        {k: v for k, v in old.items() if k not in ignored},
    ):
        document["metadata_updated"] = timestamp
        if not document.get("metadata_version") or (
            old and document["metadata_version"] == old["metadata_version"]
        ):
            document["metadata_version"] = str(uuid4())
    else:
        document["metadata_updated"] = old["metadata_updated"]
        if not document.get("metadata_version"):
            document["metadata_version"] = old["metadata_version"]
    store.save(db, "tracks", document)
