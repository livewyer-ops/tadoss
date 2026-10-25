"""Transactional notifications and leased PostgreSQL work queues."""

import copy
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from psycopg import errors, sql
from psycopg.types.json import Jsonb

from tadoss.adapters import postgres as store
from tadoss.auth import Identity
from tadoss.domain.types import Document, Job, Options
from tadoss.domain.validation import (
    SPEC,
    APIError,
    contract_schema,
    coverage,
    now,
    storage_safe,
    time_range,
    validate,
    writable,
)
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


def publish(
    db: store.Database, event_type: str, resource: Document, event: Document
) -> None:
    body = {"event_timestamp": now(), "event_type": event_type, "event": event}
    validate(
        body,
        SPEC["webhooks"][event_type]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"],
    )
    where = sql.SQL(
        "document->>'status' IN ('created','started') AND (document->'events') ? %s"
    )
    params: list[Any] = [Jsonb(body), event_type]
    is_track = event_type.startswith("tracks/")
    values = {"schema_ids": resource["schema_id"] if is_track else resource["id"]}
    if is_track:
        values.update(
            {
                "track_ids": resource["id"],
                "timeline_reference.entity_id": resource["timeline_reference"][
                    "entity_id"
                ],
            }
        )
    for key, value in values.items():
        where += sql.SQL(" AND (NOT document ? %s OR (document->%s) ? %s)")
        params.extend([key, key, value])
    if is_track:
        where += sql.SQL(
            " AND (NOT document ? 'editable_events' OR"
            " document->'editable_events' = %s)"
        )
        params.append(Jsonb(resource["editable_events"]))
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


def webhook_get(db: store.Database, identifier: str, identity: Identity) -> Document:
    row = db.execute(
        "SELECT owner, document FROM webhooks WHERE id=%s", (identifier,)
    ).fetchone()
    if row is None or (row[0] != identity.subject and not identity.admin):
        raise APIError(404, "Webhook not found")
    return row[1]


def webhook_save(
    db: store.Database,
    identifier: str,
    body: Any,
    identity: Identity,
    settings: Settings,
    replacing: bool = False,
) -> Document:
    from tadoss.adapters.webhook_delivery import validate_url

    old = webhook_get(db, identifier, identity) if replacing else None
    validate(body, contract_schema("webhook-put" if replacing else "webhook-post"))
    storage_safe(body)
    allowed = set(SPEC["components"]["schemas"]["webhook"]["properties"]) | {
        "api_key_value",
        "status",
        "id",
        "error",
    }
    if set(body) - allowed:
        raise APIError(400, "Unsupported webhook filtering or transformation")
    if replacing and body["id"] != identifier:
        raise APIError(400, "Body id must match path id")
    validate_url(body["url"], settings)
    header = body.get("api_key_name", "")
    import re

    if header and (
        not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header)
        or header.lower()
        in {"host", "content-length", "transfer-encoding", "connection"}
    ):
        raise APIError(400, "Invalid webhook authentication header")
    secret = body.get("api_key_value", "")
    if any(ord(c) < 32 or ord(c) > 126 for c in secret):
        raise APIError(400, "Webhook authentication value must be printable ASCII")
    document = {
        key: value
        for key, value in body.items()
        if key not in {"api_key_value", "error"}
    }
    document.update(id=identifier, status=body.get("status", "created"))
    if old and old["status"] == "error" and document["status"] == "disabled":
        raise APIError(400, "Re-enable an errored webhook before disabling it")
    if old is not None:
        if "api_key_value" not in body:
            secret = db.execute(
                "SELECT api_key_value FROM webhooks WHERE id=%s", (identifier,)
            ).fetchall()[0][0]
        db.execute(
            "UPDATE webhooks SET document=%s, api_key_value=%s, "
            "revision=revision+%s WHERE id=%s",
            (
                Jsonb(document),
                secret,
                int(webhook_selection(old) != webhook_selection(document)),
                identifier,
            ),
        )
    else:
        db.execute(
            "INSERT INTO webhooks (id, owner, document, api_key_value) VALUES "
            "(%s,%s,%s,%s)",
            (identifier, identity.subject, Jsonb(document), secret),
        )
    return document


def webhook_selection(document: Document) -> tuple[Any, ...]:
    # Membership order and credentials do not change which notifications match.
    return (
        document["url"],
        document["status"] in {"created", "started"},
        document.get("editable_events"),
        *(
            frozenset(document[key]) if key in document else None
            for key in (
                "events",
                "schema_ids",
                "track_ids",
                "timeline_reference.entity_id",
            )
        ),
    )


def ensure_track_available(db: store.Database, identifier: str) -> None:
    if db.execute(
        "SELECT 1 FROM jobs WHERE kind='delete' AND aggregate_id=%s AND status IN"
        " ('created','started') AND "
        "(payload->'request'->>'delete_track')::boolean LIMIT 1",
        (identifier,),
    ).fetchone():
        raise APIError(409, "Track is reserved for deletion")


def deletion_where(
    track_id: str, options: Options, cutoff: int | None = None
) -> tuple[sql.Composed, list[Any]]:
    decoded = dict(options)
    if "timerange" in decoded:
        decoded["timerange"] = time_range(str(decoded["timerange"]))
    where, params = store.event_filters(track_id, decoded, deleting=True)
    if cutoff is not None:
        where += sql.SQL(" AND mutation <= %s")
        params.append(cutoff)
    return where, params


def selected_events(
    db: store.Database,
    track_id: str,
    options: Options,
    limit: int,
    cutoff: int | None = None,
) -> list[Document]:
    where, params = deletion_where(track_id, options, cutoff)
    return [
        row[0]
        for row in db.execute(
            sql.SQL(
                "SELECT document FROM events WHERE {} ORDER BY mutation LIMIT %s"
            ).format(where),
            [*params, limit],
        )
    ]


def delete_batch(db: store.Database, track: Document, records: list[Document]) -> None:
    if not records:
        return
    identifiers = [record["id"] for record in records]
    db.execute(
        "DELETE FROM events WHERE track_id=%s AND id=ANY(%s)",
        (track["id"], identifiers),
    )
    track["events_updated"] = now()
    store.save(db, "tracks", track)
    publish(
        db,
        "tracks/events_deleted",
        track,
        {
            "track_id": track["id"],
            "track_event_ids": ",".join(identifiers),
            "timerange": coverage(records),
        },
    )


def request_delete(
    db: store.Database,
    track: Document,
    options: Options,
    delete_track: bool,
    identity: Identity,
    settings: Settings,
) -> Document | None:
    writable(track)
    ensure_track_available(db, track["id"])
    deadline = time.monotonic() + settings.delete_sync_seconds
    try:
        with db.connection.transaction():
            db.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(int(settings.delete_sync_seconds * 1000)),),
            )
            records = selected_events(
                db, track["id"], options, settings.delete_batch_size + 1
            )
            if len(records) <= settings.delete_batch_size:
                delete_batch(db, track, records)
                if delete_track:
                    store.delete(db, "tracks", track["id"])
                    publish(db, "tracks/deleted", track, {"track_id": track["id"]})
                if time.monotonic() > deadline:
                    raise errors.QueryCanceled("Synchronous deletion budget exceeded")
                db.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (settings.statement_timeout,),
                )
                return None
    except errors.QueryCanceled:
        pass
    db.execute(
        "SELECT set_config('statement_timeout', %s, true)",
        (settings.statement_timeout,),
    )
    cutoff = db.execute(
        "SELECT COALESCE(max(mutation),0) FROM events WHERE track_id=%s", (track["id"],)
    ).fetchall()[0][0]
    timestamp = now()
    timerange = str(options.get("timerange", "_"))
    request = {
        "track_id": track["id"],
        "delete_track": delete_track,
        "status": "created",
        "created": timestamp,
        "updated": timestamp,
        "created_by": identity.subject,
        "timerange_to_delete": timerange,
        "timerange_remaining": timerange,
    }
    payload = {
        "request": request,
        "options": {k: str(v) if k == "timerange" else v for k, v in options.items()},
        "cutoff": cutoff,
    }
    identifier = enqueue(db, "delete", track["id"], identity.subject, payload)
    return {**request, "id": identifier}


def delete_document(row: tuple[Any, ...]) -> Document:
    identifier, payload, status, updated, expiry, error = row
    result = {
        **payload["request"],
        "id": identifier,
        "status": payload["request"].get("status", status)
        if status == "created"
        else status,
        "updated": updated.isoformat(),
    }
    if expiry:
        result["expiry"] = expiry.isoformat()
    if error:
        result["error"] = error
    return result


def get_delete(db: store.Database, identifier: str) -> Document:
    row = db.execute(
        "SELECT id,payload,status,updated,expiry,error FROM jobs WHERE id=%s AND "
        "kind='delete'",
        (identifier,),
    ).fetchone()
    if not row:
        raise APIError(404, "Deletion request not found")
    return delete_document(row)


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


def process_delete(pool: store.Pool, job: Job, settings: Settings) -> None:
    identifier, _, track_id, payload, _, token = job
    with store.transaction(pool, write=True, lock=track_id) as db:
        if not db.execute(
            "SELECT 1 FROM jobs WHERE id=%s AND claim=%s AND lease_until>now() "
            "FOR UPDATE",
            (identifier, token),
        ).fetchone():
            return
        track = store.find(db, "tracks", track_id)
        if track is None:
            payload["request"].pop("timerange_remaining", None)
            db.execute(
                "UPDATE jobs SET payload=%s WHERE id=%s", (Jsonb(payload), identifier)
            )
            finish(db, identifier, token, "done", settings)
            return
        records = selected_events(
            db,
            track_id,
            payload["options"],
            settings.delete_batch_size,
            payload["cutoff"],
        )
        delete_batch(db, track, records)
        remaining = selected_events(
            db, track_id, payload["options"], 1, payload["cutoff"]
        )
        payload = copy.deepcopy(payload)
        payload["request"]["status"] = "started"
        if not remaining:
            payload["request"].pop("timerange_remaining", None)
            if payload["request"]["delete_track"]:
                store.delete(db, "tracks", track_id)
                publish(db, "tracks/deleted", track, {"track_id": track_id})
        else:
            where, params = deletion_where(
                track_id, payload["options"], payload["cutoff"]
            )
            first = db.execute(
                sql.SQL(
                    "SELECT sort_start,sort_start_excl FROM events WHERE {} ORDER"
                    " BY sort_start,sort_start_excl LIMIT 1"
                ).format(where),
                params,
            ).fetchone()
            last = db.execute(
                sql.SQL(
                    "SELECT sort_end,sort_end_incl FROM events WHERE {} ORDER BY "
                    "sort_end DESC,sort_end_incl DESC LIMIT 1"
                ).format(where),
                params,
            ).fetchone()
            if first is None or last is None:
                raise RuntimeError("Track changed during deletion query")
            payload["request"]["timerange_remaining"] = str(
                store.sort_keys_timerange(*first, *last)
            )
        db.execute(
            "UPDATE jobs SET payload=%s WHERE id=%s", (Jsonb(payload), identifier)
        )
        if not finish(
            db,
            identifier,
            token,
            "created" if remaining else "done",
            settings,
            reset_attempts=True,
        ):
            raise RuntimeError("Deletion lease expired; roll back this batch")


def list_deletes(db: store.Database, options: Options) -> list[Document]:
    import base64
    import binascii
    import json

    sort_by = options.get("sort_by", "created")
    if sort_by not in {"created", "expiry"}:
        raise APIError(400, "Unsupported deletion request ordering")
    reverse = options.get("reverse_order", False)
    order = sql.SQL("ASC" if reverse else "DESC")
    column = sql.Identifier(sort_by)
    # Null expiry falls after dated entries and uses a stable ID fallback.
    key = sql.SQL(
        "({0} IS NOT NULL, COALESCE({0}, '-infinity'::timestamptz), id)"
    ).format(column)
    query = sql.SQL(
        "SELECT id,payload,status,updated,expiry,error FROM jobs WHERE kind='delete'"
    )
    params = []
    if cursor := options.get("page"):
        try:
            selected_sort, selected_reverse, timestamp, identifier = json.loads(
                base64.b64decode(cursor, altchars=b"-_", validate=True)
            )
            if selected_sort != sort_by or selected_reverse is not reverse:
                raise ValueError("cursor ordering")
            if timestamp is not None:
                datetime.fromisoformat(timestamp)
            validate(identifier, contract_schema("uuid"))
        except (ValueError, TypeError, binascii.Error):
            raise APIError(400, "Invalid page cursor") from None
        query += sql.SQL(" AND {} {} (%s,%s::timestamptz,%s)").format(
            key, sql.SQL(">" if reverse else "<")
        )
        params.extend([timestamp is not None, timestamp or "-infinity", identifier])
    query += sql.SQL(" ORDER BY {} {} LIMIT %s").format(key, order)
    params.append(min(options.get("limit", 100), 1000) + 1)
    return [delete_document(row) for row in db.execute(query, params)]
