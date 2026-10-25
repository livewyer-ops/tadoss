import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg import connect, sql
from psycopg.conninfo import make_conninfo
from tadoss.adapters import postgres as store

from tests.support.api import API, SCHEMA_ID, TRACK_ID, create_app, records
from tests.support.contract import validate_response


@pytest.fixture
def database():
    url = os.getenv("TADOSS_TEST_DATABASE", store.DEFAULT_DATABASE)
    schema = "test_" + uuid4().hex
    with connect(url) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield make_conninfo(url, options=f"-c search_path={schema}")
    finally:
        with connect(url) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.fixture
def api(database):
    with store.create_pool(database) as pool:
        store.initialize(pool)
    app = create_app(database, token="test-secret")
    with TestClient(app) as client:
        client.event_hooks["response"].append(validate_response)
        client.headers["Authorization"] = "Basic dGFkb3NzOnRlc3Qtc2VjcmV0"
        schema, track = records()
        instance = API(database, app, client, schema, track)
        instance.send("PUT", f"/schemas/{SCHEMA_ID}", schema, status=201)
        instance.send("PUT", f"/tracks/{TRACK_ID}", track, status=201)
        yield instance
