"""Ordered database migrations applied by ``tadoss migrate``."""

from typing import TYPE_CHECKING, LiteralString

if TYPE_CHECKING:
    from tadoss.adapters import postgres as store


INITIAL = """
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE TABLE schemas (
    id TEXT PRIMARY KEY,
    document JSONB NOT NULL
);
CREATE INDEX schemas_document ON schemas USING gin (document);
CREATE TABLE tracks (
    id TEXT PRIMARY KEY,
    schema_id TEXT NOT NULL REFERENCES schemas(id),
    document JSONB NOT NULL
);
CREATE INDEX tracks_schema ON tracks(schema_id);
CREATE INDEX tracks_document ON tracks USING gin (document);
CREATE SEQUENCE event_mutation;
CREATE TABLE events (
    id TEXT PRIMARY KEY,
    track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    document JSONB NOT NULL,
    timerange numrange NOT NULL,
    sort_start numeric NOT NULL,
    sort_start_excl boolean NOT NULL,
    sort_end numeric NOT NULL,
    sort_end_incl boolean NOT NULL,
    mutation bigint NOT NULL DEFAULT nextval('event_mutation')
);
CREATE INDEX events_track_range ON events USING gist (track_id, timerange);
CREATE INDEX events_track_forward ON events
    (track_id, sort_start, sort_start_excl, sort_end, sort_end_incl, id);
CREATE INDEX events_track_reverse ON events (track_id, sort_end DESC,
    sort_end_incl DESC, sort_start DESC, sort_start_excl DESC, id DESC);
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
CREATE INDEX jobs_expiry ON jobs (expiry)
    WHERE status IN ('done','error') AND expiry IS NOT NULL;
"""

MIGRATIONS: list[tuple[str, LiteralString]] = [("0001_baseline", INITIAL)]


def applied(db: "store.Database") -> list[str] | None:
    """Names of applied migrations, or None when the database was never initialised."""
    if db.execute("SELECT to_regclass('tadoss_migrations')").fetchall()[0][0] is None:
        return None
    return [
        row[0] for row in db.execute("SELECT name FROM tadoss_migrations ORDER BY name")
    ]


def apply(db: "store.Database") -> None:
    done = set(applied(db) or [])
    if done - {name for name, _ in MIGRATIONS}:
        raise RuntimeError("Database migration history is incompatible with this build")
    db.execute(
        "CREATE TABLE IF NOT EXISTS tadoss_migrations"
        " (name TEXT PRIMARY KEY, applied TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    for name, step in MIGRATIONS:
        if name in done:
            continue
        db.execute(step)
        db.execute("INSERT INTO tadoss_migrations (name) VALUES (%s)", (name,))
