import psycopg
import pytest
from psycopg.errors import QueryCanceled
from tadoss.adapters import postgres as store


def test_timeout_is_transaction_local_and_reapplied(database):
    with store.create_pool(
        database, min_size=1, max_size=1, statement_timeout="20ms"
    ) as pool:
        with store.transaction(pool) as db:
            assert db.execute("SHOW statement_timeout").fetchone() == ("20ms",)
        with pool.connection() as db:
            assert db.execute("SHOW statement_timeout").fetchone() == ("0",)
        with pytest.raises(QueryCanceled), store.transaction(pool) as db:
            db.execute("SELECT pg_sleep(1)")
        with store.transaction(pool, timeout=2) as db:
            assert db.execute("SHOW statement_timeout").fetchone() == ("2s",)
        with store.transaction(pool) as db:
            assert db.execute("SHOW statement_timeout").fetchone() == ("20ms",)


def test_dead_idle_connection_is_discarded_before_a_write(database):
    with psycopg.connect(database) as admin:
        admin.execute("CREATE TABLE checkout_writes (id integer)")
    with store.create_pool(database, min_size=1, max_size=1, timeout=5) as pool:
        with pool.connection() as connection:
            pid = connection.info.backend_pid
        with psycopg.connect(database) as admin:
            assert admin.execute(
                "SELECT pg_terminate_backend(%s)", (pid,)
            ).fetchone() == (True,)
        with store.transaction(pool, write=True) as db:
            db.execute("INSERT INTO checkout_writes VALUES (1)")
        with store.transaction(pool) as db:
            assert db.execute("SELECT count(*) FROM checkout_writes").fetchone() == (1,)
