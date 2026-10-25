from dataclasses import replace
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from tadoss import worker
from tadoss.adapters import postgres as store
from tadoss.application import background
from tadoss.auth import Identity
from tadoss.domain.validation import SPEC, contract_schema, validate

from tests.support import api as test_api


def test_webhook_lifecycle_delivery_and_secret(api) -> None:
    webhook = api.register(tags={"kind": ["test"]})
    validate(webhook, contract_schema("webhook-get"))
    assert "api_key_value" not in webhook
    api.send("POST", api.events, test_api.event())
    captured = []

    def receiver(url, body, header, secret, settings):
        captured.append((body, header, secret))
        return 204

    assert worker.run_once(api.app.state.pool, api.app.state.settings, receiver)
    assert captured[0][0]["event_type"] == "tracks/events_added"
    assert captured[0][1:] == ("X-Token", "secret")
    assert (
        api.send("GET", "/service/webhooks/" + webhook["id"]).json()["status"]
        == "started"
    )
    listed = api.send("GET", "/service/webhooks", params={"tag.kind": "test"}).json()
    assert len(listed) == 1
    api.send(
        "PUT",
        "/service/webhooks/" + webhook["id"],
        {**webhook, "status": "disabled"},
        status=204,
    )
    api.send("POST", api.events, test_api.event())
    assert not worker.run_once(api.app.state.pool, api.app.state.settings, receiver)
    api.send("DELETE", "/service/webhooks/" + webhook["id"], status=204)
    api.send("GET", "/service/webhooks/" + webhook["id"], status=404)


def test_webhook_owner_and_stale_revision(api) -> None:
    webhook = api.register()
    with store.transaction(api.app.state.pool) as db:
        with pytest.raises(Exception) as error:
            background.webhook_get(db, webhook["id"], Identity("other"))
        assert error.value.status == 404
    api.send("POST", api.events, test_api.event())
    job = background.claim(api.app.state.pool, api.app.state.settings)
    api.send(
        "PUT",
        "/service/webhooks/" + webhook["id"],
        {**webhook, "status": "disabled"},
        status=204,
    )
    with patch("tadoss.worker.deliver") as deliver:
        worker.process_webhook(api.app.state.pool, job, api.app.state.settings, deliver)
        deliver.assert_not_called()


def test_expired_delete_claim_rolls_back_mutation(api) -> None:
    api.app.state.settings = replace(api.app.state.settings, delete_batch_size=1)
    api.send("POST", api.events, [test_api.event(), test_api.event()])
    api.send("DELETE", api.events, status=202)
    job = background.claim(api.app.state.pool, api.app.state.settings)
    with (
        patch("tadoss.application.background.finish", return_value=False),
        pytest.raises(RuntimeError),
    ):
        background.process_delete(api.app.state.pool, job, api.app.state.settings)
    assert len(api.send("GET", api.events).json()) == 2


def test_all_notification_types_and_retry(api) -> None:
    api.register()
    schema, track, identifier = (str(uuid4()), str(uuid4()), str(uuid4()))
    api.send(
        "PUT",
        "/schemas/" + schema,
        {"id": schema, "schema": {"type": "object"}},
        status=201,
    )
    api.send("PUT", "/schemas/" + schema + "/label", "Updated", status=204)
    api.send(
        "PUT",
        "/tracks/" + track,
        {
            "id": track,
            "schema_id": schema,
            "timeline_reference": {"entity_id": "urn:qualification"},
            "editable_events": True,
        },
        status=201,
    )
    api.send("PUT", "/tracks/" + track + "/label", "Updated", status=204)
    api.send("POST", "/tracks/" + track + "/events", test_api.event(id=identifier))
    api.send(
        "POST",
        "/tracks/" + track + "/events",
        test_api.event(id=identifier, payload={"text": "changed"}),
    )
    api.send("DELETE", "/tracks/" + track + "/events", status=204)
    api.send("DELETE", "/tracks/" + track, status=204)
    api.send("DELETE", "/schemas/" + schema, status=204)
    received = []

    def receive(_url, body, _header, _secret, _settings):
        received.append(body["event_type"])
        return 204

    while worker.run_once(api.app.state.pool, api.app.state.settings, receive):
        pass
    assert set(received) == set(SPEC["webhooks"])
    api.send("POST", api.events, test_api.event())
    assert worker.run_once(api.app.state.pool, api.app.state.settings, lambda *_: 503)
    with store.transaction(api.app.state.pool, write=True) as db:
        db.execute("UPDATE jobs SET available=now() WHERE status='created'")
    assert worker.run_once(api.app.state.pool, api.app.state.settings, receive)
    assert received[-1] == "tracks/events_added"


def test_webhook_failures_finish_subscription_and_allow_get_put_reenable(
    api, caplog, subtests
) -> None:
    import http.client

    settings = replace(api.app.state.settings, webhook_max_attempts=1)
    for failure in (
        http.client.BadStatusLine("secret bytes"),
        RuntimeError("secret bytes"),
    ):
        with subtests.test(failure=type(failure).__name__):
            webhook = api.register()
            path = "/service/webhooks/" + webhook["id"]
            api.send("POST", api.events, test_api.event())
            with caplog.at_level("INFO", logger="tadoss.worker"):
                worker.run_once(api.app.state.pool, settings, Mock(side_effect=failure))
            assert "secret bytes" not in str(caplog.messages)
            current = api.send("GET", path).json()
            assert current["status"] == "error"
            assert "secret bytes" not in str(current)
            api.send("PUT", path, {**current, "status": "created"}, status=204)
            assert "error" not in api.send("GET", path).json()
            api.send("DELETE", path, status=204)


def test_webhook_revision_preserves_metadata_credentials_and_membership_order(
    api,
) -> None:
    webhook = api.register(track_ids=[test_api.TRACK_ID, str(uuid4())])
    path = "/service/webhooks/" + webhook["id"]
    api.send("POST", api.events, test_api.event())
    changed = {
        **webhook,
        "tags": {"label": ["new"]},
        "api_key_name": "X-New",
        "api_key_value": "rotated",
        "events": list(reversed(webhook["events"])),
        "track_ids": list(reversed(webhook["track_ids"])),
    }
    api.send("PUT", path, changed, status=204)
    sender = Mock(return_value=204)
    worker.run_once(api.app.state.pool, api.app.state.settings, sender)
    assert sender.call_args.args[2:4] == ("X-New", "rotated")
    assert api.send("GET", path).json()["tags"] == {"label": ["new"]}
    api.send("POST", api.events, test_api.event())
    api.send("PUT", path, changed, status=204)
    worker.run_once(api.app.state.pool, api.app.state.settings, sender)
    assert sender.call_count == 2
    api.send("POST", api.events, test_api.event())
    api.send("PUT", path, {**changed, "status": "disabled"}, status=204)
    api.send("PUT", path, changed, status=204)
    worker.run_once(api.app.state.pool, api.app.state.settings, sender)
    assert sender.call_count == 2


def test_webhook_completion_preserves_edits_and_resets_rotated_budget(api) -> None:
    settings = replace(api.app.state.settings, webhook_max_attempts=1)
    webhook = api.register()
    path = "/service/webhooks/" + webhook["id"]
    api.send("POST", api.events, test_api.event())

    def rotate(*_args):
        api.send(
            "PUT",
            path,
            {
                **webhook,
                "tags": {"changed": ["yes"]},
                "api_key_name": "X-New",
                "api_key_value": "new-secret",
            },
            status=204,
        )
        return 401

    worker.run_once(api.app.state.pool, settings, rotate)
    current = api.send("GET", path).json()
    assert current["status"] == "started"
    assert current["tags"] == {"changed": ["yes"]}
    assert current["api_key_name"] == "X-New"
    with store.transaction(api.app.state.pool, write=True) as db:
        assert db.execute("SELECT status,attempts FROM jobs").fetchone() == (
            "created",
            0,
        )
        db.execute("UPDATE jobs SET available=now()")
    sender = Mock(return_value=204)
    worker.run_once(api.app.state.pool, settings, sender)
    assert sender.call_args.args[2:4] == ("X-New", "new-secret")


def test_webhook_filter_changes_invalidate_old_deliveries(api, subtests) -> None:
    for change in (
        {"url": "https://other.example/events"},
        {"track_ids": []},
        {"editable_events": False},
    ):
        with subtests.test(change=change):
            webhook = api.register()
            path = "/service/webhooks/" + webhook["id"]
            api.send("POST", api.events, test_api.event())
            api.send("PUT", path, {**webhook, **change}, status=204)
            sender = Mock(return_value=204)
            worker.run_once(api.app.state.pool, api.app.state.settings, sender)
            sender.assert_not_called()
            api.send("DELETE", path, status=204)


@pytest.mark.parametrize("collection", ["schemas", "tracks"])
def test_notification_selection_preserves_optional_filters_and_revisions(
    api, collection
):
    identifier = test_api.SCHEMA_ID if collection == "schemas" else test_api.TRACK_ID
    is_track = collection == "tracks"
    cases = [
        ({}, True),
        ({"status": "disabled"}, False),
        ({"events": [collection + "/deleted"]}, False),
        ({"schema_ids": [test_api.SCHEMA_ID]}, True),
        ({"schema_ids": [str(uuid4())]}, False),
        ({"schema_ids": []}, False),
        ({"track_ids": [test_api.TRACK_ID]}, True),
        ({"track_ids": [str(uuid4())]}, not is_track),
        ({"track_ids": []}, not is_track),
        ({"timeline_reference.entity_id": [test_api.ENTITY]}, True),
        ({"timeline_reference.entity_id": ["urn:other"]}, not is_track),
        ({"timeline_reference.entity_id": []}, not is_track),
        ({"editable_events": True}, True),
        ({"editable_events": False}, not is_track),
    ]
    expected = set()
    for fields, matches in cases:
        webhook = api.register(**{"events": [collection + "/updated"], **fields})
        if matches:
            expected.add(webhook["id"])
    with store.transaction(api.app.state.pool, write=True) as db:
        db.execute("UPDATE webhooks SET revision=7")
    api.send("PUT", f"/{collection}/{identifier}/label", "Changed", status=204)
    with store.transaction(api.app.state.pool) as db:
        jobs = db.execute("SELECT id,aggregate_id,owner,payload FROM jobs").fetchall()
    assert {row[1] for row in jobs} == expected
    assert len({row[0] for row in jobs}) == len(expected)
    for _, _, owner, payload in jobs:
        assert owner == "tadoss"
        assert payload["revision"] == 7
        assert payload["body"]["event_type"] == collection + "/updated"
        assert "api_key_value" not in payload
