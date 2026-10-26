import json
import os
from unittest.mock import patch
from urllib.parse import quote
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from tadoss.adapters import postgres as store
from tadoss.domain.validation import (
    SPEC,
    contract_schema,
    contract_validator,
    validate,
)
from tadoss.settings import env_flag

from tests.support.api import ENTITY, SCHEMA_ID, TRACK_ID, create_app, event
from tests.support.contract import REGISTERED_PATHS


def test_lifecycle_filters_and_persistence(api) -> None:
    validate(api.send("GET", "/service").json(), contract_schema("service"))
    created = api.send("GET", f"/tracks/{TRACK_ID}").json()
    api.send("POST", api.events, event())
    after_events = api.send("GET", f"/tracks/{TRACK_ID}").json()
    assert created["metadata_version"] == after_events["metadata_version"]
    assert created["events_updated"] != after_events["events_updated"]
    api.send("PUT", f"/tracks/{TRACK_ID}/tags/language", ["en", "fr"], status=204)
    api.send("PUT", f"/tracks/{TRACK_ID}/label", "Transcript", status=204)
    params = {
        "timeline_reference.entity_id": quote(ENTITY, safe=""),
        "tag.language": "fr",
        "tag_exists.language": "true",
        "label": "Transcript",
    }
    assert len(api.send("GET", "/tracks", params=params).json()) == 1
    assert api.send("GET", "/tracks", params={"tag.language": "f"}).json() == []
    assert (
        api.send("GET", "/tracks", params={"tag_exists.language": "false"}).json() == []
    )
    changed = api.send(
        "GET", f"/tracks/{TRACK_ID}", params={"include_timerange": "true"}
    ).json()
    assert changed["timerange"] == "[0:0_1:0)"
    assert created["metadata_version"] != changed["metadata_version"]
    api.send("PUT", f"/tracks/{TRACK_ID}", changed, status=204)
    assert (
        api.send("GET", f"/tracks/{TRACK_ID}").json()["metadata_version"]
        == changed["metadata_version"]
    )
    with TestClient(create_app(api.database, token="test-secret")) as restarted:
        assert (
            len(
                restarted.get(
                    api.events,
                    headers={"Authorization": "Basic dGFkb3NzOnRlc3Qtc2VjcmV0"},
                ).json()
            )
            == 1
        )
    api.send("DELETE", f"/schemas/{SCHEMA_ID}", status=400)
    api.send("PUT", f"/schemas/{SCHEMA_ID}", {**api.schema, "schema": {}}, status=403)
    api.send("DELETE", f"/tracks/{TRACK_ID}", status=204)
    api.send("GET", api.events, status=404)
    api.send("DELETE", f"/schemas/{SCHEMA_ID}", status=204)


def test_database_errors_hide_messages_and_classify_transient_failures(
    api, caplog
) -> None:
    with (
        patch.object(
            store, "find", side_effect=psycopg.DataError("private database value")
        ),
        caplog.at_level("INFO", logger="tadoss"),
    ):
        response = api.send("GET", f"/tracks/{TRACK_ID}", status=500)
    assert "private database value" not in str(caplog.messages) + response.text
    record = next(
        record for record in caplog.records if record.getMessage() == "Database error"
    )
    assert record.exception_type == "DataError"
    assert record.exc_info is None
    with patch.object(
        store, "find", side_effect=psycopg.errors.QueryCanceled("private query")
    ):
        response = api.send("GET", f"/tracks/{TRACK_ID}", status=503)
    assert response.headers["Retry-After"] == "1"
    assert "temporarily failed" in response.text
    assert "private query" not in response.text


def test_published_contract_matches_routes(api) -> None:
    contract = api.send("GET", "/openapi.json").json()
    assert contract["x-upstream-revision"] == SPEC["x-upstream-revision"]
    assert "/service/webhooks" in contract["paths"]
    assert set(contract["paths"]) == set(REGISTERED_PATHS)
    hits = contract_validator.cache_info().hits
    validate(str(uuid4()), contract_schema("uuid"))
    assert contract_validator.cache_info().hits > hits
    for path, operations in contract["paths"].items():
        methods = {method.upper() for method in operations if method != "parameters"}
        assert methods <= {method.upper() for method in REGISTERED_PATHS[path]}

    def walk(value):
        if isinstance(value, dict):
            if "$ref" in value:
                target = SPEC
                for part in value["$ref"][2:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(json.loads(json.dumps(SPEC)))


def test_link_header_omits_url_token(api) -> None:
    api.send("POST", api.events, [event("[0:0_1:0)"), event("[1:0_2:0)")])
    first = api.send(
        "GET",
        api.events,
        headers={"Authorization": ""},
        params={"access_token": "test-secret", "limit": 1},
    )
    link = first.headers["Link"]
    assert "access_token" not in link
    assert "test-secret" not in link
    second = api.send("GET", link.split(">", 1)[0][1:])
    assert len(second.json()) == 1
    for record in first.json() + second.json():
        assert "track_id" not in record


def test_database_errors_are_tads_errors(api, caplog) -> None:
    with (
        patch(
            "tadoss.adapters.postgres.listing",
            side_effect=psycopg.ProgrammingError("x"),
        ),
        caplog.at_level("INFO", logger="tadoss"),
    ):
        response = TestClient(api.app, raise_server_exceptions=False).get(
            "/tracks", headers={"Authorization": "Basic dGFkb3NzOnRlc3Qtc2VjcmV0"}
        )
    assert response.status_code == 500
    validate(response.json(), contract_schema("error"))


def test_pool_exhaustion_returns_503(api) -> None:
    settings = {"TADOSS_POOL_MAX_SIZE": "1", "TADOSS_POOL_TIMEOUT": "0.5"}
    with patch.dict(os.environ, settings):
        app = create_app(api.database, token="test-secret")
    with TestClient(app) as client:
        held = app.state.pool.getconn()
        try:
            response = client.get(
                "/tracks", headers={"Authorization": "Basic dGFkb3NzOnRlc3Qtc2VjcmV0"}
            )
        finally:
            app.state.pool.putconn(held)
        assert response.status_code == 503
        assert "Retry-After" in response.headers
        validate(response.json(), contract_schema("error"))
        assert client.get("/readyz").status_code == 200


def test_access_log_and_request_id(api, caplog) -> None:
    with caplog.at_level("INFO", logger="tadoss.access"):
        response = api.send(
            "GET",
            api.events,
            headers={"Authorization": ""},
            params={"access_token": "test-secret"},
        )
    line = caplog.messages[-1]
    assert "GET /tracks/{track_id}/events 200" in line
    assert "access_token" not in line
    assert "test-secret" not in line
    assert UUID(response.headers["X-Request-ID"])
    supplied = str(uuid4())
    echoed = api.send("GET", "/service", headers={"X-Request-ID": supplied})
    assert echoed.headers["X-Request-ID"] == supplied
    replaced = api.send("GET", "/service", headers={"X-Request-ID": "not a uuid"})
    assert replaced.headers["X-Request-ID"] != "not a uuid"
    with patch.dict(os.environ, {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1"}):
        app = create_app(api.database, token="test-secret")
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


def test_unhandled_error_keeps_request_id(api) -> None:
    with patch(
        "tadoss.adapters.postgres.listing", side_effect=RuntimeError("unexpected")
    ):
        client = TestClient(api.app, raise_server_exceptions=False)
        response = client.get("/tracks", headers=dict(api.client.headers))
        client.close()
    assert response.status_code == 500
    assert UUID(response.headers["X-Request-ID"])
    validate(response.json(), contract_schema("error"))


def test_dev_mode_gate(api, caplog) -> None:
    with caplog.at_level("INFO", logger="tadoss"):
        app = create_app(api.database, token="", dev=True)
    with TestClient(app) as client:
        assert client.get("/tracks").status_code == 200
    with pytest.raises(ValueError):
        create_app(api.database, token="", dev=False)
    with patch.dict(os.environ), pytest.raises(ValueError):
        os.environ.pop("TADOSS_DATABASE", None)
        os.environ.pop("TADOSS_DEV", None)
        create_app(None, token="t")


def test_docs_can_be_disabled(api) -> None:
    with patch.dict(os.environ, {"TADOSS_DOCS": "false"}):
        app = create_app(api.database, token="test-secret")
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 200
    with TestClient(api.app) as client:
        assert client.get("/docs").status_code == 200


def test_collection_cursor_survives_anchor_deletion(api) -> None:
    for value in (1, 2, 3):
        sid = str(UUID(f"00000000-0000-4000-8000-{value:012d}"))
        api.send("PUT", f"/schemas/{sid}", {"id": sid, "schema": {}}, status=201)
    first = api.send("GET", "/schemas", params={"limit": 1})
    api.send("DELETE", "/schemas/" + first.json()[0]["id"], status=204)
    second = api.send(
        "GET",
        "/schemas",
        params={"limit": 1, "page": first.headers["X-Paging-NextKey"]},
    )
    assert second.json()[0]["id"] == str(UUID("00000000-0000-4000-8000-000000000002"))


def test_env_flag() -> None:
    with patch.dict(os.environ):
        os.environ.pop("TADOSS_FLAG", None)
        assert env_flag("TADOSS_FLAG", True)
        assert not env_flag("TADOSS_FLAG", False)
        for value, expected in [
            ("1", True),
            ("true", True),
            ("YES", True),
            ("On", True),
            ("0", False),
            ("false", False),
            ("NO", False),
            ("Off", False),
        ]:
            os.environ["TADOSS_FLAG"] = value
            assert env_flag("TADOSS_FLAG", None) is expected, value
        os.environ["TADOSS_FLAG"] = "maybe"
        with pytest.raises(ValueError):
            env_flag("TADOSS_FLAG", True)


def test_discovery_without_database() -> None:
    settings = {"TADOSS_POOL_TIMEOUT": "0.5"}
    with patch.dict(os.environ, settings):
        app = create_app("postgresql://x@127.0.0.1:1/x", token="t")
    headers = {"Authorization": "Basic dGFkb3NzOnQ="}
    with TestClient(app) as client:
        assert client.get("/", headers=headers).json() == [
            "schemas",
            "service",
            "tracks",
            "track-delete-requests",
        ]
        assert client.get("/service", headers=headers).status_code == 200
        missing = client.get(f"/track-delete-requests/{uuid4()}", headers=headers)
        assert missing.status_code == 503
        validate(missing.json(), contract_schema("error"))
        unavailable = client.get("/tracks", headers=headers)
        assert unavailable.status_code == 503
        assert "Retry-After" in unavailable.headers
        validate(unavailable.json(), contract_schema("error"))
        assert client.get("/readyz").status_code == 503
        assert client.get("/healthz").status_code == 200


def test_commit_failure_rolls_back_mutation_and_notification(api) -> None:
    api.register()
    before = api.send("GET", f"/tracks/{TRACK_ID}").json()
    with psycopg.connect(api.database) as connection:
        connection.execute("""
            CREATE FUNCTION reject_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'private constraint value' USING ERRCODE = '23514';
            END $$;
            CREATE CONSTRAINT TRIGGER reject_commit
              AFTER UPDATE ON tracks DEFERRABLE INITIALLY DEFERRED
              FOR EACH ROW EXECUTE FUNCTION reject_commit();
        """)
    response = api.send("PUT", f"/tracks/{TRACK_ID}/label", "uncommitted", status=409)
    assert "private constraint value" not in response.text
    assert api.send("GET", f"/tracks/{TRACK_ID}").json() == before
    with psycopg.connect(api.database) as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (0,)


@pytest.mark.parametrize("reverse", ["true", "false"])
def test_deletion_get_and_head_report_order(api, reverse):
    get = api.send("GET", "/track-delete-requests", params={"reverse_order": reverse})
    head = api.send("HEAD", "/track-delete-requests", params={"reverse_order": reverse})
    assert head.content == b""
    for response in (get, head):
        assert response.headers["X-Paging-Reverse-Order"] == reverse
        assert response.headers["X-Paging-Count"] == "0"
