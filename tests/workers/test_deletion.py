from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from tadoss import worker
from tadoss.adapters import postgres as store
from tadoss.application import background
from tadoss.domain.validation import contract_schema, validate

from tests.support import api as test_api


def test_async_deletion_preserves_later_mutations_and_recovers_claim(
    api, request
) -> None:
    api.app.state.settings = replace(api.app.state.settings, delete_batch_size=1)
    identifiers = [str(uuid4()) for _ in range(3)]
    api.send("POST", api.events, [test_api.event(id=value) for value in identifiers])
    response = api.send("DELETE", api.events, status=202)
    request = response.json()
    validate(request, contract_schema("track-delete-request"))
    api.send(
        "POST", api.events, test_api.event(id=identifiers[0], payload={"text": "new"})
    )
    stale = background.claim(api.app.state.pool, api.app.state.settings)
    with store.transaction(api.app.state.pool, write=True) as db:
        db.execute(
            "UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s",
            (stale[0],),
        )
    job = background.claim(api.app.state.pool, api.app.state.settings)
    background.process_delete(api.app.state.pool, stale, api.app.state.settings)
    assert len(api.send("GET", api.events).json()) == 3
    background.process_delete(api.app.state.pool, job, api.app.state.settings)
    while worker.run_once(api.app.state.pool, api.app.state.settings):
        pass
    assert [x["id"] for x in api.send("GET", api.events).json()] == identifiers[:1]
    assert api.send("GET", response.headers["Location"]).json()["status"] == "done"


def test_whole_track_deletion_reserves_track(api) -> None:
    api.app.state.settings = replace(api.app.state.settings, delete_batch_size=1)
    api.send("POST", api.events, [test_api.event(), test_api.event()])
    response = api.send("DELETE", f"/tracks/{test_api.TRACK_ID}", status=202)
    api.send("POST", api.events, test_api.event(), status=409)
    while worker.run_once(api.app.state.pool, api.app.state.settings):
        pass
    api.send("GET", f"/tracks/{test_api.TRACK_ID}", status=404)
    assert api.send("GET", response.headers["Location"]).json()["status"] == "done"


def test_rejected_mutations_do_not_enqueue(api) -> None:
    api.register()
    api.send("POST", api.events, test_api.event(payload={"text": 4}))
    assert not worker.run_once(api.app.state.pool, api.app.state.settings)
    api.send(
        "POST",
        "/service/webhooks",
        {"url": "http://169.254.169.254/", "events": []},
        status=400,
    )


def test_deletion_paging_expiry_and_progress(api) -> None:
    api.app.state.settings = replace(api.app.state.settings, delete_batch_size=1)
    api.send(
        "POST",
        api.events,
        [test_api.event(timerange=f"[{n}:0_{n + 1}:0)") for n in range(3)],
    )
    requests = [api.send("DELETE", api.events, status=202).json() for _ in range(3)]
    latest = requests[-1]["id"]
    assert (
        api.send("GET", "/track-delete-requests", params={"limit": 1}).json()[0]["id"]
        == latest
    )
    with store.transaction(api.app.state.pool, write=True) as db:
        db.execute(
            "UPDATE jobs SET expiry=now()+interval '1 day' WHERE id=%s", (latest,)
        )
    seen = []
    params = {"sort_by": "expiry", "limit": 1}
    while True:
        response = api.send("GET", "/track-delete-requests", params=params)
        seen.extend(x["id"] for x in response.json())
        if "X-Paging-NextKey" not in response.headers:
            break
        params["page"] = response.headers["X-Paging-NextKey"]
    assert seen[0] == latest
    assert set(seen) == {x["id"] for x in requests}
    worker.run_once(api.app.state.pool, api.app.state.settings)
    progress = api.send("GET", "/track-delete-requests/" + requests[0]["id"]).json()
    assert progress["status"] == "started"
    assert progress["timerange_remaining"] != "_"


def test_delete_failure_budget_counts_consecutive_failed_batches(api, request) -> None:
    settings = replace(
        api.app.state.settings,
        delete_batch_size=1,
        delete_max_attempts=2,
        webhook_max_attempts=1,
    )
    api.app.state.settings = settings
    api.send("POST", api.events, [test_api.event() for _ in range(8)])
    request = api.send("DELETE", api.events, status=202).json()
    pool = api.app.state.pool
    for _ in range(5):
        worker.run_once(pool, settings)
    for _ in range(2):
        with patch.object(
            background, "process_delete", side_effect=RuntimeError("sensitive")
        ):
            worker.run_once(pool, settings)
        with store.transaction(pool, write=True) as db:
            row = db.execute(
                "SELECT status,attempts FROM jobs WHERE id=%s", (request["id"],)
            ).fetchone()
            assert row == ("created", 1)
            db.execute("UPDATE jobs SET available=now() WHERE id=%s", (request["id"],))
        worker.run_once(pool, settings)
    with patch.object(
        background, "process_delete", side_effect=RuntimeError("sensitive")
    ):
        for _ in range(2):
            worker.run_once(pool, settings)
            with store.transaction(pool, write=True) as db:
                db.execute(
                    "UPDATE jobs SET available=now() WHERE id=%s", (request["id"],)
                )
    assert (
        api.send("GET", "/track-delete-requests/" + request["id"]).json()["status"]
        == "error"
    )
