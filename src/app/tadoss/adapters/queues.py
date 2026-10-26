"""PostgreSQL queue claims, completion and retention within existing transactions."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from psycopg import sql
from psycopg.types.json import Jsonb

from tadoss.adapters import postgres as store
from tadoss.domain.types import Document, Job
from tadoss.settings import Settings


def enqueue(
    db: store.Database, kind: str, aggregate: str, owner: str, payload: Document
) -> str:
    identifier = str(uuid4())
    db.execute(
        "INSERT INTO jobs (id, kind, aggregate_id, owner, payload) VALUES "
        "(%s,%s,%s,%s,%s)",
        (identifier, kind, aggregate, owner, Jsonb(payload)),
    )
    return identifier


def claim(pool: store.Pool, settings: Settings) -> Job | None:
    token = str(uuid4())
    with store.transaction(pool, write=True) as db:
        row = db.execute(
            """WITH candidate AS (
            SELECT id FROM jobs WHERE
            (status='created' AND available<=now()) OR
            (status='started' AND lease_until<now())
            ORDER BY available, created FOR UPDATE SKIP LOCKED LIMIT 1
        ) UPDATE jobs SET status='started', claim=%s,
          lease_until=now()+make_interval(secs => %s),
          attempts=attempts+1, updated=now()
        FROM candidate WHERE jobs.id=candidate.id
        RETURNING jobs.id,kind,aggregate_id,payload,attempts,claim""",
            (token, settings.worker_lease_seconds),
        ).fetchone()
        return row


def finish(
    db: store.Database,
    identifier: str,
    token: str,
    status: str,
    settings: Settings,
    error: Document | None = None,
    delay: int = 0,
    reset_attempts: bool = False,
) -> bool:
    expiry = (
        datetime.now(UTC) + timedelta(seconds=settings.worker_queue_retention_seconds)
        if status in {"done", "error"} and settings.worker_queue_retention_seconds
        else None
    )
    return (
        db.execute(
            "UPDATE jobs SET "
            "status=%s,error=%s,claim=NULL,lease_until=NULL,updated=now(),available=now()+make_interval(secs"
            " => %s),expiry=%s,attempts=CASE WHEN %s THEN 0 ELSE attempts END "
            "WHERE id=%s AND claim=%s AND lease_until>now() "
            "RETURNING id",
            (
                status,
                Jsonb(error) if error else None,
                delay,
                expiry,
                reset_attempts,
                identifier,
                token,
            ),
        ).fetchone()
        is not None
    )


def renew(db: store.Database, job: Job, lease_seconds: int) -> bool:
    return (
        db.execute(
            "UPDATE jobs SET lease_until=now()+make_interval(secs => %s) "
            "WHERE id=%s AND claim=%s AND lease_until>now() RETURNING id",
            (lease_seconds, job[0], job[5]),
        ).fetchone()
        is not None
    )


def maintain(db: store.Database) -> list[tuple[str, int, float]]:
    # Purge bounded batches so retention cannot monopolise the database.
    db.execute(
        "DELETE FROM jobs WHERE id IN (SELECT id FROM jobs WHERE expiry<now()"
        " AND status IN ('done','error') LIMIT 1000)"
    )
    return [
        (kind, count, float(age))
        for kind, count, age in db.execute(
            "SELECT kind,count(*),extract(epoch FROM now()-min(created)) "
            "FROM jobs WHERE status IN ('created','started') GROUP BY kind"
        )
    ]


def enqueue_notifications(
    db: store.Database,
    body: Document,
    event_type: str,
    values: dict[str, str],
    editable_events: bool | None,
) -> None:
    where = sql.SQL(
        "document->>'status' IN ('created','started') AND (document->'events') ? %s"
    )
    params: list[Any] = [Jsonb(body), event_type]
    for key, value in values.items():
        where += sql.SQL(" AND (NOT document ? %s OR (document->%s) ? %s)")
        params.extend([key, key, value])
    if editable_events is not None:
        where += sql.SQL(
            " AND (NOT document ? 'editable_events' OR"
            " document->'editable_events' = %s)"
        )
        params.append(Jsonb(editable_events))
    # PostgreSQL selects subscribers and copies the shared body in one statement.
    db.execute(
        sql.SQL(
            "INSERT INTO jobs (id, kind, aggregate_id, owner, payload)"
            " SELECT gen_random_uuid()::text, 'webhook', id, owner,"
            " jsonb_build_object('body', %s::jsonb, 'revision', revision)"
            " FROM webhooks WHERE {}"
        ).format(where),
        params,
    )
