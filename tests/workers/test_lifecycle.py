from unittest.mock import patch

from tadoss import worker
from tadoss.adapters import postgres as store
from tadoss.application import background
from tadoss.settings import Settings


def test_worker_drains_full_batches_without_poll_delay():
    import threading
    from unittest.mock import MagicMock

    stopped = threading.Event()
    waits = []
    calls = []

    def process(*_args):
        calls.append(True)
        if len(calls) == 3:
            assert waits == []
            stopped.set()
        return True

    with (
        patch.object(worker.threading, "Event", return_value=stopped),
        patch.object(stopped, "wait", side_effect=lambda delay: waits.append(delay)),
        patch.object(worker, "start_worker_http_server"),
        patch.object(worker.signal, "signal"),
        patch.object(store, "create_pool", return_value=MagicMock()),
        patch.object(store, "transaction", return_value=MagicMock()),
        patch.object(store, "check_schema"),
        patch.object(worker, "run_once", side_effect=process),
        patch.object(worker, "maintain") as maintain,
    ):
        worker.main(Settings(dev=True, worker_max_requests=2))
    assert len(calls) == 3
    maintain.assert_called_once()


def test_retention_is_bounded_and_preserves_active_and_retained_jobs(database):
    with store.create_pool(database) as pool:
        store.initialize(pool)
        with store.transaction(pool, write=True) as db:
            db.execute(
                "INSERT INTO jobs(id,kind,aggregate_id,owner,payload,status,expiry)"
                " SELECT gen_random_uuid()::text,'webhook','test','test','{}','done',"
                " now()-interval '1 day' FROM generate_series(1,1002)"
            )
            for identifier, status, expiry in (
                ("active", "created", "2000-01-01"),
                ("retained", "error", None),
                ("future", "done", "2100-01-01"),
            ):
                db.execute(
                    "INSERT INTO jobs(id,kind,aggregate_id,owner,payload,status,expiry)"
                    " VALUES (%s,'webhook','test','test','{}',%s,%s)",
                    (identifier, status, expiry),
                )
        settings = Settings(worker_queue_retention_seconds=0)
        job = background.claim(pool, settings)
        assert job[0] == "active"
        with store.transaction(pool, write=True) as db:
            assert background.finish(db, job[0], job[5], "done", settings)
        worker.maintain(pool)
        with store.transaction(pool) as db:
            assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 5
        worker.maintain(pool)
        with store.transaction(pool) as db:
            assert db.execute("SELECT id FROM jobs ORDER BY id").fetchall() == [
                ("active",),
                ("future",),
                ("retained",),
            ]


def test_worker_readiness_recovers_after_schema_failure_and_stops_on_exit():
    import threading
    from unittest.mock import MagicMock

    stopped = threading.Event()

    def wait(_delay):
        if not stopped.is_set():
            health = server.call_args.args[1]
            assert health.is_live()
            assert not health.is_ready()

    def process(*_args):
        assert server.call_args.args[1].is_ready()
        stopped.set()
        return False

    with (
        patch.object(worker.threading, "Event", return_value=stopped),
        patch.object(stopped, "wait", side_effect=wait),
        patch.object(worker, "start_worker_http_server") as server,
        patch.object(worker.signal, "signal"),
        patch.object(store, "create_pool", return_value=MagicMock()),
        patch.object(store, "transaction", return_value=MagicMock()),
        patch.object(
            store, "check_schema", side_effect=[RuntimeError("schema missing"), None]
        ),
        patch.object(worker, "run_once", side_effect=process),
        patch.object(worker, "maintain"),
    ):
        worker.main(Settings(dev=True))
    health = server.call_args.args[1]
    assert not health.is_live()
    assert not health.is_ready()
    server.return_value.close.assert_called_once()
