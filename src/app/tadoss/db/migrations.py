"""Ordered database migrations applied by ``tadoss migrate`` and at start-up."""

INITIAL = """
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


def event_ranges(db):
    """Store event timeranges as PostgreSQL ranges with sort keys, indexed per track."""
    from tadoss.adapters.postgres import (
        range_columns,  # deferred: store imports this module
    )
    from tadoss.domain.validation import time_range

    db.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    db.execute(
        "ALTER TABLE events"
        " ADD COLUMN timerange numrange,"
        " ADD COLUMN sort_start numeric,"
        " ADD COLUMN sort_start_excl boolean,"
        " ADD COLUMN sort_end numeric,"
        " ADD COLUMN sort_end_incl boolean"
    )
    with db.connection.cursor(name="tadoss_event_ranges") as rows:
        rows.execute("SELECT id, document->>'timerange' FROM events")
        while batch := rows.fetchmany(1000):
            db.executemany(
                "UPDATE events SET timerange = %s, sort_start = %s,"
                " sort_start_excl = %s, sort_end = %s, sort_end_incl = %s"
                " WHERE id = %s",
                [
                    (*range_columns(time_range(value)), identifier)
                    for identifier, value in batch
                ],
            )
    db.execute(
        "ALTER TABLE events"
        " ALTER COLUMN timerange SET NOT NULL,"
        " ALTER COLUMN sort_start SET NOT NULL,"
        " ALTER COLUMN sort_start_excl SET NOT NULL,"
        " ALTER COLUMN sort_end SET NOT NULL,"
        " ALTER COLUMN sort_end_incl SET NOT NULL"
    )
    db.execute(
        "UPDATE events SET document = document - 'track_id' WHERE document ? 'track_id'"
    )
    db.execute(
        "CREATE INDEX events_track_range ON events USING gist (track_id, timerange)"
    )
    db.execute(
        "CREATE INDEX events_track_forward ON events"
        " (track_id, sort_start, sort_start_excl, sort_end, sort_end_incl, id)"
    )
    db.execute(
        "CREATE INDEX events_track_reverse ON events (track_id, sort_end DESC,"
        " sort_end_incl DESC, sort_start DESC, sort_start_excl DESC, id DESC)"
    )
    db.execute("DROP INDEX IF EXISTS events_track")


MIGRATIONS = [("0001_initial", INITIAL), ("0002_event_ranges", event_ranges)]


def applied(db):
    """Names of applied migrations, or None when the database was never initialised."""
    if db.execute("SELECT to_regclass('tadoss_migrations')").fetchone()[0] is None:
        return None
    return [
        row[0] for row in db.execute("SELECT name FROM tadoss_migrations ORDER BY name")
    ]


def apply(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS tadoss_migrations"
        " (name TEXT PRIMARY KEY, applied TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    done = set(applied(db) or [])
    for name, step in MIGRATIONS:
        if name in done:
            continue
        if callable(step):
            step(db)
        else:
            db.execute(step)
        db.execute("INSERT INTO tadoss_migrations (name) VALUES (%s)", (name,))


BACKGROUND = """
CREATE SEQUENCE event_mutation;
ALTER TABLE events ADD COLUMN mutation bigint NOT NULL
    DEFAULT nextval('event_mutation');
CREATE INDEX events_track_mutation ON events (track_id, mutation);
CREATE TABLE webhooks (
    id text PRIMARY KEY,
    owner text NOT NULL,
    document jsonb NOT NULL,
    api_key_value text NOT NULL DEFAULT '',
    revision bigint NOT NULL DEFAULT 1
);
CREATE TABLE jobs (
    id text PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('delete', 'webhook')),
    aggregate_id text NOT NULL,
    owner text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'created',
    attempts integer NOT NULL DEFAULT 0,
    available timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz,
    claim text,
    created timestamptz NOT NULL DEFAULT now(),
    updated timestamptz NOT NULL DEFAULT now(),
    expiry timestamptz,
    error jsonb
);
CREATE INDEX jobs_pending ON jobs (available, created)
    WHERE status IN ('created', 'started');
CREATE INDEX jobs_aggregate ON jobs (kind, aggregate_id, status);
CREATE INDEX schemas_document ON schemas USING gin (document);
CREATE INDEX tracks_document ON tracks USING gin (document);
"""
MIGRATIONS.append(("0003_background", BACKGROUND))
