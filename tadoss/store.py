import os
from contextlib import contextmanager
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from .validation import (
    APIError,
    check_grid,
    contract_schema,
    event_key,
    now,
    payload_validator,
    time_range,
    validate,
    validate_payload,
)

SCHEMA_VERSION = 1
DEFAULT_DATABASE = "postgresql://tadoss:tadoss@127.0.0.1:15432/tadoss"
STATEMENT_TIMEOUT = os.getenv("TADOSS_STATEMENT_TIMEOUT", "30s")
DDL = """
CREATE TABLE IF NOT EXISTS tadoss_version (version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS schemas (
    id TEXT PRIMARY KEY,
    document JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY,
    schema_id TEXT NOT NULL REFERENCES schemas(id),
    document JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS tracks_schema ON tracks(schema_id);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    document JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS events_track ON events(track_id);
"""


def initialize(database):
    with transaction(database, write=True) as db:
        db.execute(DDL)
        db.execute(
            "INSERT INTO tadoss_version VALUES (%s) ON CONFLICT DO NOTHING",
            (SCHEMA_VERSION,),
        )
        check_schema(db)


def check_schema(db):
    versions = db.execute("SELECT version FROM tadoss_version").fetchall()
    if versions != [(SCHEMA_VERSION,)]:
        raise RuntimeError("Unsupported database schema version")


@contextmanager
def transaction(database, write=False):
    with (
        psycopg.connect(database, connect_timeout=10) as connection,
        connection.cursor() as db,
    ):
        if write:
            db.execute("SET LOCAL lock_timeout = '10s'")
        else:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        # SET rejects bound parameters; set_config with is_local=true is equivalent.
        db.execute(
            "SELECT set_config('statement_timeout', %s, true)", (STATEMENT_TIMEOUT,)
        )
        if write:
            db.execute("SELECT pg_advisory_xact_lock(1413563475)")
        yield db


def get(db, table, identifier, required=True):
    row = db.execute(
        f"SELECT document FROM {table} WHERE id = %s", (identifier,)
    ).fetchone()
    if row is None:
        if required:
            raise APIError(404, "Resource not found")
        return None
    document = row[0]
    document.pop("track_id", None)
    return document


def listing(db, table):
    return [row[0] for row in db.execute(f"SELECT document FROM {table} ORDER BY id")]


def events(db, track_id):
    result = []
    for (document,) in db.execute(
        "SELECT document FROM events WHERE track_id = %s", (track_id,)
    ):
        document.pop("track_id", None)
        result.append(document)
    return result


def event(db, identifier):
    row = db.execute(
        "SELECT track_id, document FROM events WHERE id = %s", (identifier,)
    ).fetchone()
    if row is None:
        return None
    track_id, document = row
    document.pop("track_id", None)
    return track_id, document


def delete_events(db, track_id, ids):
    db.execute(
        "DELETE FROM events WHERE track_id = %s AND id = ANY(%s)",
        (track_id, list(ids)),
    )
    return db.rowcount


def save(db, table, document, track_id=None):
    columns = ["id", "document"]
    values = [document["id"], Jsonb(document)]
    if table == "tracks":
        columns.append("schema_id")
        values.append(document["schema_id"])
    elif table == "events":
        columns.append("track_id")
        values.append(track_id)
    assignments = ", ".join(f"{column}=excluded.{column}" for column in columns[1:])
    placeholders = ", ".join("%s" for _ in columns)
    db.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {assignments}",
        values,
    )


def writable(track):
    if track["read_only"]:
        raise APIError(403, "Track is read-only")


def save_schema(db, document, old=None):
    validate(document, contract_schema("schema"))
    payload_validator(document["schema"])
    if old and old["schema"] != document["schema"]:
        raise APIError(403, "A payload schema's schema is immutable")
    timestamp = now()
    document["tags"] = document.get("tags", {})
    document["created"] = old["created"] if old else timestamp
    document["updated"] = timestamp
    document["created_by"] = old["created_by"] if old else "tadoss"
    document["updated_by"] = "tadoss"
    save(db, "schemas", document)


def save_track(db, document, old=None, unlocking=False):
    validate(document, contract_schema("track"))
    if old and not unlocking:
        writable(old)
    get(db, "schemas", document["schema_id"])
    for key, default in (
        ("tags", {}),
        ("read_only", False),
        ("allow_overlaps", True),
        ("editable_events", True),
    ):
        document.setdefault(key, default)
    document.pop("timerange", None)
    if old:
        if old["schema_id"] != document["schema_id"]:
            raise APIError(400, "A track's schema_id is immutable")
        track_events = events(db, old["id"])
        if track_events and old["timeline_reference"] != document["timeline_reference"]:
            raise APIError(
                400, "Cannot change the timeline reference of a populated track"
            )
        for record in track_events:
            check_grid(time_range(record["timerange"]), document)
        if not document["allow_overlaps"]:
            previous = None
            for record in sorted(track_events, key=event_key):
                current = time_range(record["timerange"])
                if current.is_empty():
                    continue
                if previous is not None and previous.overlaps_with_timerange(current):
                    raise APIError(400, "Track already contains overlapping events")
                previous = current
    timestamp = now()
    document["created"] = old["created"] if old else timestamp
    document["created_by"] = old["created_by"] if old else "tadoss"
    document["updated_by"] = "tadoss"
    document["events_updated"] = old["events_updated"] if old else timestamp
    ignored = {"metadata_version", "metadata_updated", "events_updated"}
    changed = old is None or {
        k: v for k, v in document.items() if k not in ignored
    } != {k: v for k, v in old.items() if k not in ignored}
    document["metadata_updated"] = timestamp if changed else old["metadata_updated"]
    if not document.get("metadata_version") or (
        old and changed and document["metadata_version"] == old["metadata_version"]
    ):
        document["metadata_version"] = (
            str(uuid4()) if changed else old["metadata_version"]
        )
    save(db, "tracks", document)


def write_events(db, track, body):
    writable(track)
    batch = body if isinstance(body, list) else [body]
    if (
        not batch
        or len(batch) > 1000
        or any(not isinstance(record, dict) for record in batch)
    ):
        raise APIError(
            400, "Supply an event object or an array of 1 to 1000 event objects"
        )
    validator = payload_validator(get(db, "schemas", track["schema_id"])["schema"])
    responses = []
    changed = False
    for incoming in batch:
        result = {}
        if "id" in incoming:
            result["id"] = incoming["id"]
        try:
            validate(incoming, contract_schema("track-event-post"))
            record = {**incoming, "id": incoming.get("id", str(uuid4()))}
            result["id"] = record["id"]
            existing = event(db, record["id"])
            if existing and existing[0] != track["id"]:
                raise APIError(400, "Event ID already belongs to another track")
            if existing and not track["editable_events"]:
                raise APIError(403, "Track does not permit editing existing events")
            timerange = time_range(record["timerange"])
            check_grid(timerange, track)
            validate_payload(validator, record["payload"])
            if not track["allow_overlaps"]:
                if any(
                    other["id"] != record["id"]
                    and timerange.overlaps_with_timerange(
                        time_range(other["timerange"])
                    )
                    for other in events(db, track["id"])
                ):
                    raise APIError(400, "Event overlaps an existing event")
            record["timerange"] = str(timerange)
            save(db, "events", record, track_id=track["id"])
            result["http_status_code"] = 200 if existing else 201
            changed = True
        except APIError as exc:
            result.update(http_status_code=exc.status, error=exc.body)
        responses.append(result)
    if changed:
        track["events_updated"] = now()
        save(db, "tracks", track)
    return {
        "all_successful": all(r["http_status_code"] < 300 for r in responses),
        "responses": responses,
    }
