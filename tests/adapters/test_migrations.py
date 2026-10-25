from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg import connect, sql
from psycopg.errors import DuplicateTable
from tadoss.adapters import postgres as store
from tadoss.db import migrations

from tests.support.api import SCHEMA_ID, TRACK_ID, create_app, event


def test_initial_migration_rolls_back_and_retries(database):
    with connect(database) as connection:
        connection.execute("CREATE TABLE jobs (id text PRIMARY KEY)")
        connection.execute("INSERT INTO jobs VALUES ('existing')")
    app = create_app(database, token="t")
    with TestClient(app) as client:
        assert client.get("/readyz").json()["reason"] == "schema"
        with pytest.raises(DuplicateTable):
            store.initialize(app.state.pool)
        with connect(database) as connection:
            assert connection.execute("SELECT id FROM jobs").fetchall() == [
                ("existing",)
            ]
            assert connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            ).fetchall() == [("jobs",)]
            assert connection.execute(
                "SELECT to_regclass('event_mutation')"
            ).fetchone() == (None,)
            connection.execute("DROP TABLE jobs")
        assert client.get("/readyz").status_code == 503
        store.initialize(app.state.pool)
        store.initialize(app.state.pool)
        assert client.get("/readyz").status_code == 200
        with store.transaction(app.state.pool) as db:
            assert migrations.applied(db) == ["0001_baseline"]
            assert db.execute("SELECT to_regclass('jobs_expiry')").fetchone() == (
                "jobs_expiry",
            )


def test_repeated_migration_preserves_populated_baseline(api):
    webhook = api.register()
    document = event(id=str(uuid4()))
    assert api.send("POST", api.events, [document]).json()["all_successful"]
    with store.transaction(api.app.state.pool) as db:
        before = {
            table: db.execute(
                sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
            ).fetchall()
            for table in ("schemas", "tracks", "events", "webhooks", "jobs")
        }
    assert before["jobs"]
    store.initialize(api.app.state.pool)
    store.initialize(api.app.state.pool)
    with store.transaction(api.app.state.pool) as db:
        for table, rows in before.items():
            assert (
                db.execute(
                    sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
                ).fetchall()
                == rows
            )
    assert api.send("GET", api.events).json() == [document]
    assert api.send("GET", "/service/webhooks").json() == [webhook]


@pytest.mark.parametrize("history", ["0001_initial", "0002_future", None])
def test_api_rejects_unready_schema_without_mutating_data(api, history):
    schema = api.send("GET", f"/schemas/{SCHEMA_ID}").json()
    track = api.send("GET", f"/tracks/{TRACK_ID}").json()
    with connect(api.database) as connection:
        if history is None:
            connection.execute("DROP TABLE tadoss_migrations")
        else:
            connection.execute("UPDATE tadoss_migrations SET name = %s", (history,))
    assert api.client.get("/readyz").status_code == 503
    api.send("GET", "/schemas", status=503)
    api.send(
        "PUT", f"/tracks/{TRACK_ID}", {**api.track, "label": "blocked"}, status=503
    )
    api.send("POST", api.events, event(), status=503)
    api.send("DELETE", f"/schemas/{SCHEMA_ID}", status=503)
    api.send("GET", "/schemas", status=401, headers={"Authorization": "Basic invalid"})
    with connect(api.database) as connection:
        assert (
            connection.execute("SELECT document FROM schemas").fetchone()[0] == schema
        )
        assert connection.execute("SELECT document FROM tracks").fetchone()[0] == track
        assert connection.execute("SELECT count(*) FROM events").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (0,)


@pytest.mark.parametrize(
    "history",
    [
        ["0001_initial"],
        ["0001_initial", "0002_event_ranges", "0003_background", "0004_job_retention"],
        ["0001_baseline", "0002_future"],
    ],
)
def test_incompatible_history_is_rejected_without_changes(database, history):
    with connect(database) as connection:
        connection.execute(
            "CREATE TABLE tadoss_migrations (name text PRIMARY KEY,"
            " applied timestamptz NOT NULL DEFAULT now())"
        )
        connection.execute("CREATE TABLE schemas (id text PRIMARY KEY, document jsonb)")
        connection.execute("INSERT INTO schemas VALUES ('keep', '{}')")
        with connection.cursor() as db:
            db.executemany(
                "INSERT INTO tadoss_migrations (name) VALUES (%s)",
                [(name,) for name in history],
            )
    app = create_app(database, token="t")
    with TestClient(app) as client:
        assert client.get("/readyz").status_code == 503
        with pytest.raises(RuntimeError, match="migration history is incompatible"):
            store.initialize(app.state.pool)
        with store.transaction(app.state.pool) as db:
            with pytest.raises(RuntimeError, match="migration history is incompatible"):
                store.check_schema(db)
            assert migrations.applied(db) == history
            assert db.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
                " ORDER BY tablename"
            ).fetchall() == [("schemas",), ("tadoss_migrations",)]
            assert db.execute("SELECT * FROM schemas").fetchall() == [("keep", {})]
