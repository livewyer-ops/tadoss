from uuid import uuid4

import psycopg
import pytest
from prometheus_client import REGISTRY
from tadoss import worker

from tests.support.api import TRACK_ID, event


def outcomes():
    return {
        outcome: REGISTRY.get_sample_value("tadoss_events_total", {"outcome": outcome})
        or 0
        for outcome in ("created", "updated", "rejected")
    }


@pytest.mark.parametrize("editable", [True, False])
def test_event_metrics_count_individual_results_after_commit(api, editable):
    identifier = str(uuid4())
    api.send("POST", api.events, event(id=identifier))
    api.send("PUT", f"/tracks/{TRACK_ID}/editable_events", editable, status=204)
    api.register()
    before = outcomes()
    batch_size = REGISTRY.get_sample_value("tadoss_event_batch_size_sum") or 0
    added = event(id=str(uuid4()))
    updated = event(id=identifier, payload={"text": "updated"})
    result = api.send(
        "POST",
        api.events,
        [added, updated, event(payload={"text": 12})],
    ).json()
    assert [row["http_status_code"] for row in result["responses"]] == [
        201,
        200 if editable else 403,
        400,
    ]
    assert not result["all_successful"]
    assert outcomes() == {
        "created": before["created"] + 1,
        "updated": before["updated"] + int(editable),
        "rejected": before["rejected"] + 1 + int(not editable),
    }
    assert REGISTRY.get_sample_value("tadoss_event_batch_size_sum") == batch_size + 3
    delivered = {}

    def receive(_url, body, _header, _secret, _settings):
        delivered[body["event_type"]] = body["event"]["track_events"]
        return 204

    while worker.run_once(api.app.state.pool, api.app.state.settings, receive):
        pass
    expected = {"tracks/events_added": [added]}
    if editable:
        expected["tracks/events_updated"] = [updated]
    assert delivered == expected
    stored = {record["id"]: record for record in api.send("GET", api.events).json()}
    assert stored == {
        added["id"]: added,
        identifier: updated if editable else event(id=identifier),
    }


def test_commit_failure_does_not_count_successful_events(api):
    api.register()
    before_track = api.send("GET", f"/tracks/{TRACK_ID}").json()
    before = outcomes()
    with psycopg.connect(api.database) as connection:
        connection.execute("""
            CREATE FUNCTION reject_event_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'rollback' USING ERRCODE = '23514'; END $$;
            CREATE CONSTRAINT TRIGGER reject_event_commit
              AFTER INSERT ON events DEFERRABLE INITIALLY DEFERRED
              FOR EACH ROW EXECUTE FUNCTION reject_event_commit();
        """)
    api.send("POST", api.events, [event(), event()], status=409)
    assert api.send("GET", api.events).json() == []
    assert api.send("GET", f"/tracks/{TRACK_ID}").json() == before_track
    assert outcomes() == before
    with psycopg.connect(api.database) as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
