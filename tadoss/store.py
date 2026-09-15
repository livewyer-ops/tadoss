import os
from contextlib import contextmanager
from decimal import Decimal
from uuid import uuid4

from mediatimestamp.immutable import TimeRange, Timestamp
from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg.types.range import Range
from psycopg_pool import ConnectionPool

from . import migrations
from .validation import (
    APIError,
    check_grid,
    check_grid_timestamps,
    contract_schema,
    now,
    payload_validator,
    time_range,
    validate,
    validate_payload,
)

DEFAULT_DATABASE = "postgresql://tadoss:tadoss@127.0.0.1:15432/tadoss"
# Tables addressed by name from the API; anything else is a programming error.
TABLES = frozenset({"schemas", "tracks"})
STATEMENT_TIMEOUT = os.getenv("TADOSS_STATEMENT_TIMEOUT", "30s")
# Advisory lock namespace: the single-key form serialises migrations, the two-key
# form with a hashed resource id serialises writes to one track or schema.
LOCK_SPACE = 1413563475
FORWARD = ("sort_start", "sort_start_excl", "sort_end", "sort_end_incl", "id")
REVERSE = ("sort_end", "sort_end_incl", "sort_start", "sort_start_excl", "id")


def create_pool(database, min_size=1, max_size=10, timeout=30.0):
    return ConnectionPool(
        database,
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
        open=False,
        kwargs={"connect_timeout": 10},
        name="tadoss",
    )


def initialize(pool):
    with pool.connection() as connection, connection.cursor() as db:
        db.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_SPACE,))
        migrations.apply(db)
        check_schema(db)


def check_schema(db):
    names = migrations.applied(db)
    expected = [name for name, _ in migrations.MIGRATIONS]
    if names is None or any(name not in names for name in expected):
        raise RuntimeError("Database migration pending")
    if any(name not in expected for name in names):
        raise RuntimeError("Database is newer than this build")


@contextmanager
def transaction(pool, write=False, lock=None, timeout=None):
    with pool.connection(timeout=timeout) as connection, connection.cursor() as db:
        if write:
            db.execute("SET LOCAL lock_timeout = '10s'")
        else:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        # SET rejects bound parameters; set_config with is_local=true is equivalent.
        db.execute(
            "SELECT set_config('statement_timeout', %s, true)", (STATEMENT_TIMEOUT,)
        )
        if write:
            db.execute(
                "SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (LOCK_SPACE, lock)
            )
        yield db


def range_columns(timerange):
    """Range and sort keys of a TimeRange, in the order of the events columns."""
    if timerange.is_empty():
        return Range(empty=True), Decimal(0), True, Decimal(0), False
    start = None if timerange.start is None else Decimal(timerange.start.to_nanosec())
    end = None if timerange.end is None else Decimal(timerange.end.to_nanosec())
    bounds = ("[" if timerange.includes_start() else "(") + (
        "]" if timerange.includes_end() else ")"
    )
    return (
        Range(start, end, bounds),
        Decimal("-Infinity") if start is None else start,
        not timerange.includes_start(),
        Decimal("Infinity") if end is None else end,
        timerange.includes_end(),
    )


def sort_keys_timerange(start, start_excl, end, end_incl):
    lower = (
        None if start == Decimal("-Infinity") else Timestamp.from_nanosec(int(start))
    )
    upper = None if end == Decimal("Infinity") else Timestamp.from_nanosec(int(end))
    inclusivity = TimeRange.EXCLUSIVE
    if not start_excl:
        inclusivity |= TimeRange.INCLUDE_START
    if end_incl:
        inclusivity |= TimeRange.INCLUDE_END
    return TimeRange(lower, upper, inclusivity)


def table_name(table):
    if table not in TABLES:
        raise ValueError(f"Unknown table {table!r}")
    return sql.Identifier(table)


def find(db, table, identifier):
    """The document with this id, or None."""
    row = db.execute(
        sql.SQL("SELECT document FROM {} WHERE id = %s").format(table_name(table)),
        (identifier,),
    ).fetchone()
    return None if row is None else row[0]


def get(db, table, identifier):
    """The document with this id; 404 when absent."""
    document = find(db, table, identifier)
    if document is None:
        raise APIError(404, "Resource not found")
    return document


def listing(db, table, overlapping=None):
    query = sql.SQL("SELECT document FROM {}").format(table_name(table))
    params = []
    if overlapping is not None:
        query += sql.SQL(
            " WHERE EXISTS (SELECT 1 FROM events e WHERE e.track_id = {}.id"
            " AND e.timerange && %s)"
        ).format(table_name(table))
        params.append(range_columns(overlapping)[0])
    return [row[0] for row in db.execute(query + sql.SQL(" ORDER BY id"), params)]


def event_filters(track_id, options, deleting=False):
    clauses = [sql.SQL("track_id = %s")]
    params = [track_id]
    if "ids" in options:
        clauses.append(sql.SQL("id = ANY(%s)"))
        params.append(options["ids"])
    if "timerange" in options:
        clauses.append(sql.SQL("timerange <@ %s" if deleting else "timerange && %s"))
        params.append(range_columns(options["timerange"])[0])
    return sql.SQL(" AND ").join(clauses), params


def events(db, track_id, options, limit, cursor=None, reverse=False):
    """Up to ``limit`` events in TADS order after ``cursor`` as (document, key)."""
    where, params = event_filters(track_id, options)
    keys = REVERSE if reverse else FORWARD
    columns = sql.SQL(", ").join(map(sql.Identifier, keys))
    if cursor is not None:
        where = sql.SQL("{} AND ({}) {} ({})").format(
            where,
            columns,
            sql.SQL("<" if reverse else ">"),
            sql.SQL(", ").join(sql.Placeholder() * len(keys)),
        )
        params.extend(cursor)
    direction = sql.SQL("DESC" if reverse else "ASC")
    order = sql.SQL(", ").join(
        sql.SQL("{} {}").format(sql.Identifier(key), direction) for key in keys
    )
    query = sql.SQL(
        "SELECT document, {} FROM events WHERE {} ORDER BY {} LIMIT %s"
    ).format(columns, where, order)
    return [(row[0], row[1:]) for row in db.execute(query, [*params, limit])]


def event(db, identifier):
    row = db.execute(
        "SELECT track_id, document FROM events WHERE id = %s", (identifier,)
    ).fetchone()
    return None if row is None else (row[0], row[1])


def delete_events(db, track_id, options):
    timerange = options.get("timerange")
    if timerange is not None and timerange.is_empty():
        return 0
    where, params = event_filters(track_id, options, deleting=True)
    db.execute(sql.SQL("DELETE FROM events WHERE {}").format(where), params)
    return db.rowcount


def upsert_event(db, track_id, record, timerange):
    """Insert or replace an event; False when the id belongs to another track."""
    db.execute(
        "INSERT INTO events (id, track_id, document, timerange,"
        " sort_start, sort_start_excl, sort_end, sort_end_incl)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (id) DO UPDATE SET document = excluded.document,"
        " timerange = excluded.timerange, sort_start = excluded.sort_start,"
        " sort_start_excl = excluded.sort_start_excl, sort_end = excluded.sort_end,"
        " sort_end_incl = excluded.sort_end_incl"
        " WHERE events.track_id = excluded.track_id",
        (record["id"], track_id, Jsonb(record), *range_columns(timerange)),
    )
    return db.rowcount == 1


def overlapping_exists(db, track_id, timerange, exclude_id):
    return (
        db.execute(
            "SELECT 1 FROM events WHERE track_id = %s AND id <> %s AND timerange && %s"
            " LIMIT 1",
            (track_id, exclude_id, range_columns(timerange)[0]),
        ).fetchone()
        is not None
    )


def track_has_events(db, track_id):
    return (
        db.execute(
            "SELECT 1 FROM events WHERE track_id = %s LIMIT 1", (track_id,)
        ).fetchone()
        is not None
    )


def track_has_overlaps(db, track_id):
    return (
        db.execute(
            "SELECT 1 FROM events a JOIN events b ON b.track_id = a.track_id"
            " AND b.id > a.id AND b.timerange && a.timerange"
            " WHERE a.track_id = %s LIMIT 1",
            (track_id,),
        ).fetchone()
        is not None
    )


def track_bounds(db, track_id):
    """Distinct finite event boundaries of a track."""
    rows = db.execute(
        "SELECT DISTINCT bound FROM (SELECT lower(timerange) AS bound FROM events"
        " WHERE track_id = %s UNION SELECT upper(timerange) FROM events"
        " WHERE track_id = %s) bounds WHERE bound IS NOT NULL",
        (track_id, track_id),
    )
    return [Timestamp.from_nanosec(int(value)) for (value,) in rows]


def track_coverage(db, track_id, overlapping=None):
    """Timerange string spanning a track's events, optionally only those overlapping."""
    options = {} if overlapping is None else {"timerange": overlapping}
    where, params = event_filters(track_id, options)
    where = sql.SQL("{} AND NOT isempty(timerange)").format(where)
    first = db.execute(
        sql.SQL(
            "SELECT sort_start, sort_start_excl FROM events WHERE {}"
            " ORDER BY sort_start, sort_start_excl LIMIT 1"
        ).format(where),
        params,
    ).fetchone()
    if first is None:
        return str(TimeRange.never())
    last = db.execute(
        sql.SQL(
            "SELECT sort_end, sort_end_incl FROM events WHERE {}"
            " ORDER BY sort_end DESC, sort_end_incl DESC LIMIT 1"
        ).format(where),
        params,
    ).fetchone()
    return str(sort_keys_timerange(first[0], first[1], last[0], last[1]))


def save(db, table, document):
    columns = ["id", "document"]
    values = [document["id"], Jsonb(document)]
    if table == "tracks":
        columns.append("schema_id")
        values.append(document["schema_id"])
    assignments = sql.SQL(", ").join(
        sql.SQL("{0} = excluded.{0}").format(sql.Identifier(column))
        for column in columns[1:]
    )
    db.execute(
        sql.SQL(
            "INSERT INTO {} ({}) VALUES ({}) ON CONFLICT (id) DO UPDATE SET {}"
        ).format(
            table_name(table),
            sql.SQL(", ").join(map(sql.Identifier, columns)),
            sql.SQL(", ").join(sql.Placeholder() * len(columns)),
            assignments,
        ),
        values,
    )


def delete(db, table, identifier):
    db.execute(
        sql.SQL("DELETE FROM {} WHERE id = %s").format(table_name(table)), (identifier,)
    )


def schema_in_use(db, schema_id):
    return (
        db.execute(
            "SELECT 1 FROM tracks WHERE schema_id = %s LIMIT 1", (schema_id,)
        ).fetchone()
        is not None
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
        populated = track_has_events(db, old["id"])
        if populated and old["timeline_reference"] != document["timeline_reference"]:
            raise APIError(
                400, "Cannot change the timeline reference of a populated track"
            )
        if populated and document.get("edit_rate"):
            check_grid_timestamps(track_bounds(db, old["id"]), document)
        if not document["allow_overlaps"] and track_has_overlaps(db, old["id"]):
            raise APIError(400, "Track already contains overlapping events")
    timestamp = now()
    document["created"] = old["created"] if old else timestamp
    document["created_by"] = old["created_by"] if old else "tadoss"
    document["updated_by"] = "tadoss"
    document["events_updated"] = old["events_updated"] if old else timestamp
    ignored = {"metadata_version", "metadata_updated", "events_updated"}
    if old is None or {k: v for k, v in document.items() if k not in ignored} != {
        k: v for k, v in old.items() if k not in ignored
    }:
        document["metadata_updated"] = timestamp
        if not document.get("metadata_version") or (
            old and document["metadata_version"] == old["metadata_version"]
        ):
            document["metadata_version"] = str(uuid4())
    else:
        document["metadata_updated"] = old["metadata_updated"]
        if not document.get("metadata_version"):
            document["metadata_version"] = old["metadata_version"]
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
            timerange = time_range(record["timerange"], require_non_empty=True)
            check_grid(timerange, track)
            validate_payload(validator, record["payload"])
            if not track["allow_overlaps"] and overlapping_exists(
                db, track["id"], timerange, record["id"]
            ):
                raise APIError(400, "Event overlaps an existing event")
            record["timerange"] = str(timerange)
            # A savepoint keeps one rejected event from discarding the others.
            with db.connection.transaction():
                if not upsert_event(db, track["id"], record, timerange):
                    raise APIError(400, "Event ID already belongs to another track")
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
