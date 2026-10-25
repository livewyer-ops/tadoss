from collections.abc import Callable, Iterator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any
from urllib.parse import unquote

from mediatimestamp.immutable import TimeRange, Timestamp
from psycopg import Connection, Cursor, sql
from psycopg.types.json import Jsonb
from psycopg.types.range import Range
from psycopg_pool import ConnectionPool

from tadoss.db import migrations
from tadoss.domain.types import Document, EventCursor, Options
from tadoss.domain.validation import (
    APIError,
)

type Database = Cursor[tuple[Any, ...]]
type Pool = ConnectionPool[Connection[tuple[Any, ...]]]

DEFAULT_DATABASE = "postgresql://tadoss:tadoss@127.0.0.1:15432/tadoss"
# Tables addressed by name from the API; anything else is a programming error.
TABLES = frozenset({"schemas", "tracks", "webhooks"})
# Advisory lock namespace: the single-key form serialises migrations, the two-key
# form with a hashed resource id serialises writes to one track or schema.
LOCK_SPACE = 1413563475
FORWARD = ("sort_start", "sort_start_excl", "sort_end", "sort_end_incl", "id")
REVERSE = ("sort_end", "sort_end_incl", "sort_start", "sort_start_excl", "id")


def create_pool(
    database: str,
    min_size: int = 1,
    max_size: int = 10,
    timeout: float = 30.0,
    configure: Callable[[Connection[tuple[Any, ...]]], None] | None = None,
    statement_timeout: str = "30s",
) -> Pool:
    def configure_connection(connection: Connection[tuple[Any, ...]]) -> None:
        connection.execute(
            "SELECT set_config('statement_timeout', %s, false)", (statement_timeout,)
        )
        connection.commit()
        if configure:
            configure(connection)

    return ConnectionPool(
        database,
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
        open=False,
        kwargs={"connect_timeout": 10},
        name="tadoss",
        configure=configure_connection,
    )


def initialize(pool: Pool) -> None:
    with pool.connection() as connection, connection.cursor() as db:
        db.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_SPACE,))
        migrations.apply(db)
        check_schema(db)


def check_schema(db: Database) -> None:
    names = migrations.applied(db)
    expected = [name for name, _ in migrations.MIGRATIONS]
    if names is not None and any(name not in expected for name in names):
        raise RuntimeError("Database migration history is incompatible with this build")
    if names is None or any(name not in names for name in expected):
        raise RuntimeError("Database migration pending")


@contextmanager
def transaction(
    pool: Pool,
    write: bool = False,
    lock: str | None = None,
    timeout: float | None = None,
) -> Iterator[Database]:
    with pool.connection(timeout=timeout) as connection, connection.cursor() as db:
        if write:
            db.execute("SET LOCAL lock_timeout = '10s'")
        else:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        if timeout is not None:
            db.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(int(timeout * 1000)),),
            )
        if write:
            db.execute(
                "SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (LOCK_SPACE, lock)
            )
        yield db


def range_columns(
    timerange: TimeRange,
) -> tuple[Range[Decimal], Decimal, bool, Decimal, bool]:
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


def sort_keys_timerange(
    start: Decimal, start_excl: bool, end: Decimal, end_incl: bool
) -> TimeRange:
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


def table_name(table: str) -> sql.Identifier:
    if table not in TABLES:
        raise ValueError(f"Unknown table {table!r}")
    return sql.Identifier(table)


def find(db: Database, table: str, identifier: str) -> Document | None:
    """The document with this id, or None."""
    row = db.execute(
        sql.SQL("SELECT document FROM {} WHERE id = %s").format(table_name(table)),
        (identifier,),
    ).fetchone()
    return None if row is None else row[0]


def get(db: Database, table: str, identifier: str) -> Document:
    """The document with this id; 404 when absent."""
    document = find(db, table, identifier)
    if document is None:
        raise APIError(404, "Resource not found")
    return document


def listing(
    db: Database, table: str, options: Options, limit: int, after: str | None = None
) -> list[Document]:
    query = sql.SQL("SELECT document FROM {} WHERE TRUE").format(table_name(table))
    params = []
    overlapping = options.get("timerange")
    if overlapping is not None:
        query += sql.SQL(
            " AND EXISTS (SELECT 1 FROM events e WHERE e.track_id = {}.id"
            " AND e.timerange && %s)"
        ).format(table_name(table))
        params.append(range_columns(overlapping)[0])
    for key, value in options.items():
        if key == "_owner":
            query += sql.SQL(" AND owner = %s")
            params.append(value)
        elif key.startswith("tag_exists."):
            query += sql.SQL(" AND COALESCE((document->'tags') ? %s, false) = %s")
            params.extend([key[11:], value])
        elif key.startswith("tag."):
            query += sql.SQL(" AND document @> ANY(%s)")
            params.append(
                [
                    Jsonb({"tags": {key[4:]: member}})
                    for item in value.split(",")
                    for member in (item, [item])
                ]
            )
        elif key == "timeline_reference.entity_id":
            query += sql.SQL(" AND document @> ANY(%s)")
            params.append(
                [
                    Jsonb({"timeline_reference": {"entity_id": unquote(uri)}})
                    for uri in value.split(",")
                ]
            )
        elif key == "schema_id":
            query += sql.SQL(" AND schema_id = %s")
            params.append(value)
        elif key in {"label", "external_id", "editable_events"}:
            query += sql.SQL(" AND document @> %s")
            params.append(Jsonb({key: value}))
    if after is not None:
        query += sql.SQL(" AND id > %s")
        params.append(after)
    params.append(limit)
    return [
        row[0] for row in db.execute(query + sql.SQL(" ORDER BY id LIMIT %s"), params)
    ]


def event_filters(
    track_id: str, options: Options, deleting: bool = False
) -> tuple[sql.Composed, list[Any]]:
    clauses = [sql.SQL("track_id = %s")]
    params: list[Any] = [track_id]
    if "ids" in options:
        clauses.append(sql.SQL("id = ANY(%s)"))
        params.append(options["ids"])
    if "timerange" in options:
        clauses.append(sql.SQL("timerange <@ %s" if deleting else "timerange && %s"))
        params.append(range_columns(options["timerange"])[0])
    return sql.SQL(" AND ").join(clauses), params


def events(
    db: Database,
    track_id: str,
    options: Options,
    limit: int,
    cursor: EventCursor | None = None,
    reverse: bool = False,
) -> list[tuple[Document, EventCursor]]:
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


def event(db: Database, identifier: str) -> tuple[str, Document] | None:
    row = db.execute(
        "SELECT track_id, document FROM events WHERE id = %s", (identifier,)
    ).fetchone()
    return None if row is None else (row[0], row[1])


def delete_events(db: Database, track_id: str, options: Options) -> int:
    timerange = options.get("timerange")
    if timerange is not None and timerange.is_empty():
        return 0
    where, params = event_filters(track_id, options, deleting=True)
    db.execute(sql.SQL("DELETE FROM events WHERE {}").format(where), params)
    return db.rowcount


def upsert_event(
    db: Database, track_id: str, record: Document, timerange: TimeRange
) -> bool:
    """Insert or replace an event; False when the id belongs to another track."""
    db.execute(
        "INSERT INTO events (id, track_id, document, timerange,"
        " sort_start, sort_start_excl, sort_end, sort_end_incl)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (id) DO UPDATE SET document = excluded.document,"
        " timerange = excluded.timerange, sort_start = excluded.sort_start,"
        " sort_start_excl = excluded.sort_start_excl, sort_end = excluded.sort_end,"
        " sort_end_incl = excluded.sort_end_incl, mutation=nextval('event_mutation')"
        " WHERE events.track_id = excluded.track_id",
        (record["id"], track_id, Jsonb(record), *range_columns(timerange)),
    )
    return db.rowcount == 1


def overlapping_exists(
    db: Database, track_id: str, timerange: TimeRange, exclude_id: str
) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM events WHERE track_id = %s AND id <> %s AND timerange && %s"
            " LIMIT 1",
            (track_id, exclude_id, range_columns(timerange)[0]),
        ).fetchone()
        is not None
    )


def track_has_events(db: Database, track_id: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM events WHERE track_id = %s LIMIT 1", (track_id,)
        ).fetchone()
        is not None
    )


def track_has_overlaps(db: Database, track_id: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM events a JOIN events b ON b.track_id = a.track_id"
            " AND b.id > a.id AND b.timerange && a.timerange"
            " WHERE a.track_id = %s LIMIT 1",
            (track_id,),
        ).fetchone()
        is not None
    )


def track_bounds(db: Database, track_id: str) -> list[Timestamp]:
    """Distinct finite event boundaries of a track."""
    rows = db.execute(
        "SELECT DISTINCT bound FROM (SELECT lower(timerange) AS bound FROM events"
        " WHERE track_id = %s UNION SELECT upper(timerange) FROM events"
        " WHERE track_id = %s) bounds WHERE bound IS NOT NULL",
        (track_id, track_id),
    )
    return [Timestamp.from_nanosec(int(value)) for (value,) in rows]


def track_coverage(
    db: Database, track_id: str, overlapping: TimeRange | None = None
) -> str:
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
    if last is None:
        raise RuntimeError("Track changed during coverage query")
    return str(sort_keys_timerange(first[0], first[1], last[0], last[1]))


def save(db: Database, table: str, document: Document) -> None:
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


def delete(db: Database, table: str, identifier: str) -> None:
    db.execute(
        sql.SQL("DELETE FROM {} WHERE id = %s").format(table_name(table)), (identifier,)
    )


def schema_in_use(db: Database, schema_id: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM tracks WHERE schema_id = %s LIMIT 1", (schema_id,)
        ).fetchone()
        is not None
    )


def event_documents(db: Database, identifiers: list[str]) -> list[dict[str, Any]]:
    return [
        row[0]
        for row in db.execute(
            "SELECT document FROM events WHERE id=ANY(%s)", (identifiers,)
        )
    ]
