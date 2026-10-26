from uuid import uuid4

import psycopg
from prometheus_client import REGISTRY

from tests.support.api import event


def outcomes():
    return {
        outcome: REGISTRY.get_sample_value("tadoss_events_total", {"outcome": outcome})
        or 0
        for outcome in ("created", "updated", "rejected")
    }


def test_event_metrics_count_individual_results_after_commit(api):
    identifier = str(uuid4())
    api.send("POST", api.events, event(id=identifier))
    before = outcomes()
    batch_size = REGISTRY.get_sample_value("tadoss_event_batch_size_sum") or 0
    result = api.send(
        "POST",
        api.events,
        [event(), event(id=identifier), event(payload={"text": 12})],
    ).json()
    assert [row["http_status_code"] for row in result["responses"]] == [201, 200, 400]
    assert outcomes() == {key: value + 1 for key, value in before.items()}
    assert REGISTRY.get_sample_value("tadoss_event_batch_size_sum") == batch_size + 3


def test_commit_failure_does_not_count_successful_events(api):
    before = outcomes()
    with psycopg.connect(api.database) as connection:
        connection.execute("""
            CREATE FUNCTION reject_event_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'rollback' USING ERRCODE = '23514'; END $$;
            CREATE CONSTRAINT TRIGGER reject_event_commit
              AFTER INSERT ON events DEFERRABLE INITIALLY DEFERRED
              FOR EACH ROW EXECUTE FUNCTION reject_event_commit();
        """)
    api.send("POST", api.events, event(), status=409)
    assert api.send("GET", api.events).json() == []
    assert outcomes() == before
