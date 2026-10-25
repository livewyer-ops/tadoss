"""Recoverable deletion and webhook delivery worker."""

import http.client
import logging
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from psycopg.types.json import Jsonb

from tadoss import metrics
from tadoss.adapters import postgres as store
from tadoss.adapters.webhook_delivery import deliver
from tadoss.application import background
from tadoss.domain.types import Document, Job
from tadoss.domain.validation import APIError
from tadoss.settings import Settings
from tadoss.worker_health import WorkerHealthState, start_worker_http_server

logger = logging.getLogger("tadoss.worker")


@contextmanager
def renew_claim(pool: store.Pool, job: Job, settings: Settings) -> Iterator[None]:
    stopped = threading.Event()

    def renew() -> None:
        while not stopped.wait(settings.worker_lease_seconds / 3):
            try:
                with store.transaction(pool, write=True) as db:
                    row = db.execute(
                        "UPDATE jobs SET lease_until=now()+make_interval(secs => "
                        "%s) WHERE id=%s AND claim=%s AND lease_until>now() "
                        "RETURNING id",
                        (settings.worker_lease_seconds, job[0], job[5]),
                    ).fetchone()
                    if not row:
                        return
            except Exception as exc:
                log_failure("Could not renew worker claim", exc, job)
                return

    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=2)


def log_failure(message: str, exc: Exception, job: Job | None = None) -> None:
    logger.warning(
        message,
        extra={
            "exception_type": type(exc).__name__,
            "sqlstate": getattr(exc, "sqlstate", None),
            "job_id": job[0] if job else None,
            "kind": job[1] if job else None,
        },
    )


def complete_webhook(
    pool: store.Pool,
    job: Job,
    settings: Settings,
    error: Document | None = None,
    *,
    terminal: bool = False,
    credentials: tuple[str, str] | None = None,
) -> None:
    identifier, _, webhook_id, payload, attempts, token = job
    with store.transaction(pool, write=True, lock=webhook_id) as db:
        row = db.execute(
            "SELECT document,api_key_value,revision FROM webhooks WHERE id=%s",
            (webhook_id,),
        ).fetchone()
        active = (
            row is not None
            and row[2] == payload["revision"]
            and row[0]["status"] in {"created", "started"}
        )
        rotated = (
            row is not None
            and active
            and credentials is not None
            and credentials != (row[0].get("api_key_name", ""), row[1])
        )
        if error and rotated:
            attempts, terminal = 0, False
        state = (
            "done"
            if error is None or not active
            else "error"
            if terminal or attempts >= settings.webhook_max_attempts
            else "created"
        )
        if (
            background.finish(
                db,
                identifier,
                token,
                state,
                settings,
                error,
                delay=min(600, 5 * 2 ** min(attempts, 7)) if error else 0,
                reset_attempts=bool(error and rotated),
            )
            and active
        ):
            patch = {
                "status": "error" if state == "error" else "started",
                **({"error": error} if state == "error" else {}),
            }
            db.execute(
                "UPDATE webhooks SET document=(document-'error') || %s WHERE id=%s",
                (Jsonb(patch), webhook_id),
            )
    metrics.WORK.labels("webhook", state).inc()


def process_webhook(
    pool: store.Pool,
    job: Job,
    settings: Settings,
    sender: Callable[[str, Document, str, str, Settings], int] = deliver,
) -> None:
    _, _, webhook_id, payload, _, _ = job
    with store.transaction(pool) as db:
        row = db.execute(
            "SELECT document,api_key_value,revision FROM webhooks WHERE id=%s",
            (webhook_id,),
        ).fetchone()
    if (
        row is None
        or row[2] != payload["revision"]
        or row[0]["status"] not in {"created", "started"}
    ):
        complete_webhook(pool, job, settings)
        return
    document, secret, _ = row
    credentials = (document.get("api_key_name", ""), secret)
    error, terminal = None, False
    try:
        with renew_claim(pool, job, settings):
            status = sender(document["url"], payload["body"], *credentials, settings)
        if not 200 <= status < 300:
            error = APIError(502, f"Webhook receiver returned HTTP {status}").body
    except APIError:
        error, terminal = APIError(400, "Webhook destination rejected").body, True
    except (OSError, ValueError, http.client.HTTPException) as exc:
        log_failure("Webhook connection failed", exc, job)
        error = APIError(502, "Webhook connection failed").body
    except Exception as exc:
        log_failure("Webhook delivery failed", exc, job)
        error = APIError(500, "Webhook delivery failed").body
    complete_webhook(
        pool, job, settings, error, terminal=terminal, credentials=credentials
    )


def run_once(
    pool: store.Pool,
    settings: Settings,
    sender: Callable[[str, Document, str, str, Settings], int] = deliver,
) -> bool:
    job = background.claim(pool, settings)
    if job is None:
        return False
    try:
        if job[1] == "delete":
            background.process_delete(pool, job, settings)
            metrics.WORK.labels("delete", "processed").inc()
        else:
            process_webhook(pool, job, settings, sender)
    except Exception as exc:
        log_failure("Background operation failed", exc, job)
        error = APIError(500, "Background operation failed").body
        if job[1] == "webhook":
            complete_webhook(pool, job, settings, error)
        else:
            with store.transaction(pool, write=True) as db:
                state = "error" if job[4] >= settings.delete_max_attempts else "created"
                background.finish(db, job[0], job[5], state, settings, error, delay=30)
        metrics.WORK.labels(job[1], "failed").inc()
    return True


def maintain(pool: store.Pool) -> None:
    with store.transaction(pool, write=True) as db:
        # Purge bounded batches so retention cannot monopolise the database.
        db.execute(
            "DELETE FROM jobs WHERE id IN (SELECT id FROM jobs WHERE expiry<now()"
            " AND status IN ('done','error') LIMIT 1000)"
        )
        for kind in ("delete", "webhook"):
            count, age = db.execute(
                "SELECT count(*), COALESCE(extract(epoch FROM "
                "now()-min(created)),0) FROM jobs WHERE kind=%s AND status IN "
                "('created','started')",
                (kind,),
            ).fetchall()[0]
            metrics.QUEUE.labels(kind).set(count)
            metrics.AGE.labels(kind).set(float(age))


def main(settings: Settings) -> None:
    settings.database_url()
    stopped = threading.Event()
    health = WorkerHealthState(max(60, settings.worker_lease_seconds * 2))
    server = start_worker_http_server(settings, health)

    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_args: stopped.set())
        with store.create_pool(
            settings.database_url(),
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            timeout=settings.pool_timeout,
            statement_timeout=settings.statement_timeout,
        ) as pool:
            next_maintenance = 0.0
            while not stopped.is_set():
                full_batch = False
                try:
                    health.mark_poll_started()
                    with store.transaction(pool) as db:
                        store.check_schema(db)
                    health.mark_poll_succeeded()
                    for _ in range(settings.worker_max_requests):
                        health.mark_poll_started()
                        if stopped.is_set() or not run_once(pool, settings):
                            break
                    else:
                        full_batch = True
                    if time.monotonic() >= next_maintenance:
                        maintain(pool)
                        next_maintenance = (
                            time.monotonic() + settings.worker_poll_interval_seconds
                        )
                except Exception as exc:
                    full_batch = False
                    health.mark_poll_failed()
                    log_failure("Worker is waiting for its database and schema", exc)
                else:
                    health.mark_poll_succeeded()
                if not full_batch:
                    stopped.wait(settings.worker_poll_interval_seconds)
    finally:
        health.mark_stopping()
        server.close()
