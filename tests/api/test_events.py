import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from tadoss.adapters import postgres as store
from tadoss.domain.validation import (
    contract_schema,
    validate,
)

from tests.support.api import TRACK_ID, event
from tests.support.events import event_key


def test_bulk_validation_editing_and_global_ids(api) -> None:
    identifier = str(uuid4())
    response = api.send(
        "POST",
        api.events,
        [
            event(id=identifier),
            event(payload={"text": 5}),
            {"payload": {}},
            event("[1:0_2:0)"),
        ],
    ).json()
    assert not response["all_successful"]
    assert [r["http_status_code"] for r in response["responses"]] == [
        201,
        400,
        400,
        201,
    ]
    validate(response, contract_schema("track-event-post-responses"))
    edited = api.send(
        "POST", api.events, event(id=identifier, payload={"text": "Corrected"})
    ).json()
    assert edited["responses"][0]["http_status_code"] == 200
    other = str(uuid4())
    api.send("PUT", f"/tracks/{other}", {**api.track, "id": other}, status=201)
    duplicate = api.send("POST", f"/tracks/{other}/events", event(id=identifier)).json()
    assert duplicate["responses"][0]["http_status_code"] == 400
    api.send("PUT", f"/tracks/{TRACK_ID}/editable_events", False, status=204)
    immutable = api.send("POST", api.events, event(id=identifier)).json()
    assert immutable["responses"][0]["http_status_code"] == 403
    api.send("DELETE", api.events, params={"ids": identifier}, status=204)
    assert (
        api.send("POST", api.events, event(id=identifier)).json()["responses"][0][
            "http_status_code"
        ]
        == 201
    )


def test_batch_ownership_prefetch_preserves_partial_success_and_duplicates(api) -> None:
    for editable in (True, False):
        api.send("PUT", f"/tracks/{TRACK_ID}/editable_events", editable, status=204)
        identifier = str(uuid4())
        batch = [
            event(id=identifier),
            event(id=identifier),
            event(id="bad\x00id"),
            event(id="bad\ud800id"),
            event(id=123),
            event(),
        ]
        with patch.object(
            store, "event", side_effect=AssertionError("per-record ownership lookup")
        ):
            response = api.client.post(
                api.events,
                content=json.dumps(batch),
                headers={"Content-Type": "application/json"},
            )
        assert response.status_code == 200, response.text
        assert [r["http_status_code"] for r in response.json()["responses"]] == [
            201,
            200 if editable else 403,
            400,
            400,
            400,
            201,
        ]


def test_boundaries_sorting_pagination_and_contained_deletion(api) -> None:
    inputs = [
        event("[2:0_20:0)"),
        event("[0:0_10:0)"),
        event("[1:0_2:0)"),
        event("[1:0]"),
        event("[-0:1_0:0)"),
        event("[10000000000:1_10000000000:2)"),
    ]
    api.send("POST", api.events, inputs)
    reverse = api.send("GET", api.events, params={"reverse_order": "true"}).json()
    assert [e["timerange"] for e in reverse] == [
        inputs[i]["timerange"] for i in [5, 0, 1, 2, 3, 4]
    ]
    assert len(api.send("GET", api.events, params={"timerange": "[1:0]"}).json()) == 3
    first = api.send("GET", api.events, params={"limit": 2})
    all_events = first.json()
    while "Link" in first.headers:
        first = api.send("GET", first.headers["Link"].split(">", 1)[0][1:])
        all_events.extend(first.json())
    assert len({e["id"] for e in all_events}) == 6
    head = api.send("HEAD", api.events, params={"limit": 2})
    assert head.content == b""
    assert head.headers["X-Paging-Count"] == "2"
    api.send("DELETE", api.events, params={"timerange": "[1:0_2:0)"}, status=204)
    assert len(api.send("GET", api.events).json()) == 4
    assert (
        len(api.send("GET", "/tracks", params={"timerange": "[11:0_12:0)"}).json()) == 1
    )
    assert api.send("GET", "/tracks", params={"timerange": "[21:0_22:0)"}).json() == []
    for timerange in ["_", "()", "_0:0]", "(0:0_", "[0:0_0:0)"]:
        api.send("GET", api.events, params={"timerange": timerange})


def test_overlap_grid_and_atomic_concurrent_writes(api) -> None:
    api.send(
        "PUT",
        f"/tracks/{TRACK_ID}",
        {**api.track, "allow_overlaps": False, "edit_rate": {"numerator": 25}},
        status=204,
    )
    response = api.send(
        "POST",
        api.events,
        [event("[0:0_1:0)"), event("[1:0_2:0)"), event("[0:1_0:2)"), event("[1:0]")],
    ).json()
    assert [r["http_status_code"] for r in response["responses"]] == [
        201,
        201,
        400,
        400,
    ]
    api.send("DELETE", api.events, status=204)

    def write(_):
        return api.client.post(api.events, json=event()).json()["responses"][0][
            "http_status_code"
        ]

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(write, range(4))) == [201, 400, 400, 400]
    api.send("PUT", f"/tracks/{TRACK_ID}/allow_overlaps", True, status=204)
    api.send("POST", api.events, event())
    api.send("PUT", f"/tracks/{TRACK_ID}/allow_overlaps", False, status=400)
    api.send("DELETE", api.events, status=204)
    api.send(
        "PUT",
        f"/tracks/{TRACK_ID}",
        {**api.track, "edit_rate": {"numerator": 30000, "denominator": 1001}},
        status=204,
    )
    aligned = api.send("POST", api.events, event("[0:0_0:33366666)")).json()
    assert aligned["all_successful"]


def test_request_boundaries_and_auth(api) -> None:
    for value in [
        "not-a-range",
        "[01:0_2:0)",
        "[0:1000000000]",
        "(1:0)",
        "[281474976710656:0]",
        "[0:0]\n",
    ]:
        api.send("GET", api.events, params={"timerange": value}, status=400)
    for params in [
        {"limit": 0},
        {"page": "garbage"},
        {"reverse_order": "yes"},
        {"ids": "invalid"},
        {"unexpected": "1"},
    ]:
        api.send("GET", api.events, params=params, status=400)
    api.send("GET", "/tracks/not-a-uuid", status=400)
    api.send("POST", api.events, [], status=400)
    api.send(
        "PUT", f"/tracks/{TRACK_ID}", {**api.track, "id": str(uuid4())}, status=400
    )
    for raw in ['{"payload":NaN}', '{"payload":1e999}', "{bad json"]:
        api.send(
            "POST",
            api.events,
            content=raw,
            headers={"Content-Type": "application/json"},
            status=400,
        )
    api.send(
        "POST",
        api.events,
        content="x" * (1024 * 1024 + 1),
        headers={"Content-Type": "application/json"},
        status=413,
    )
    api.send("POST", api.events, content="{}", status=415)
    api.send("GET", "/tracks", headers={"Authorization": "Bearer wrong"}, status=401)
    api.send("GET", "/tracks", headers={"Authorization": ""}, status=401)
    api.send(
        "GET",
        "/tracks",
        headers={"Authorization": ""},
        params={"access_token": "test-secret"},
    )


def test_delete_is_scoped_to_track(api) -> None:
    other = str(uuid4())
    api.send("PUT", f"/tracks/{other}", {**api.track, "id": other}, status=201)
    kept = api.send("POST", f"/tracks/{other}/events", event()).json()
    kept_id = kept["responses"][0]["id"]
    api.send("POST", api.events, event())
    api.send("DELETE", api.events, params={"ids": kept_id}, status=204)
    remaining = api.send("GET", f"/tracks/{other}/events").json()
    assert [record["id"] for record in remaining] == [kept_id]
    assert "track_id" not in remaining[0]


def test_order_matches_library_key(api) -> None:
    inputs = ["_", "_1:0]", "[5:0_", "[1:0]", "[0:0_10:0)", "[2:0_20:0)", "(1:0_2:0]"]
    api.send("POST", api.events, [event(value) for value in inputs])
    documents = api.send("GET", api.events).json()
    assert len(documents) == len(inputs)
    for reverse in (False, True):
        expected = [
            record["id"]
            for record in sorted(
                documents, key=lambda e: event_key(e, reverse), reverse=reverse
            )
        ]
        listed = api.send(
            "GET", api.events, params={"reverse_order": str(reverse).lower()}
        )
        assert [record["id"] for record in listed.json()] == expected
        assert listed.headers["X-Paging-Timerange"] == "_"


def test_keyset_paging_survives_concurrent_inserts(api) -> None:
    api.send("POST", api.events, [event(f"[{n}:0_{n + 1}:0)") for n in range(5)])
    first = api.send("GET", api.events, params={"limit": 2})
    before_cursor = api.send(
        "POST", api.events, event("[0:500000000_0:600000000)")
    ).json()["responses"][0]["id"]
    after_cursor = api.send(
        "POST", api.events, event("[1:500000000_1:600000000)")
    ).json()["responses"][0]["id"]
    seen = [record["id"] for record in first.json()]
    page = first
    while "Link" in page.headers:
        page = api.send("GET", page.headers["Link"].split(">", 1)[0][1:])
        seen.extend(record["id"] for record in page.json())
    assert len(seen) == len(set(seen))
    assert after_cursor in seen
    assert before_cursor not in seen
    assert len(seen) == 6
    backwards = api.send(
        "GET", api.events, params={"limit": 3, "reverse_order": "true"}
    )
    ids = [record["id"] for record in backwards.json()]
    while "Link" in backwards.headers:
        backwards = api.send("GET", backwards.headers["Link"].split(">", 1)[0][1:])
        ids.extend(record["id"] for record in backwards.json())
    assert len(ids) == 7
    assert len(set(ids)) == 7
    forward_key = first.headers["X-Paging-NextKey"]
    api.send(
        "GET",
        api.events,
        params={"page": forward_key, "reverse_order": "true"},
        status=400,
    )
    api.send("GET", api.events, params={"page": "eyJ2IjoyfQ"}, status=400)


def test_empty_timeranges(api) -> None:
    response = api.send("POST", api.events, [event("[1:0_1:0)"), event("()")])
    assert [r["http_status_code"] for r in response.json()["responses"]] == [400, 400]
    api.send("POST", api.events, event())
    assert api.send("GET", api.events, params={"timerange": "()"}).json() == []
    api.send("DELETE", api.events, params={"timerange": "()"}, status=204)
    assert len(api.send("GET", api.events).json()) == 1


def test_same_id_on_two_tracks_concurrently(api) -> None:
    other = str(uuid4())
    api.send("PUT", f"/tracks/{other}", {**api.track, "id": other}, status=201)
    identifier = str(uuid4())

    def post(path):
        response = api.client.post(path, json=event(id=identifier))
        return response.json()["responses"][0]["http_status_code"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(post, [api.events, f"/tracks/{other}/events"]))
    assert codes == [201, 400]


def test_coverage_and_track_filters_with_unbounded_events(api) -> None:
    api.send("POST", api.events, [event("_1:0]"), event("[5:0]")])
    track = api.send(
        "GET", f"/tracks/{TRACK_ID}", params={"include_timerange": "true"}
    ).json()
    assert track["timerange"] == "_5:0]"
    params = {"include_timerange": "true", "timerange": "[4:0_6:0)"}
    assert (
        api.send("GET", f"/tracks/{TRACK_ID}", params=params).json()["timerange"]
        == "[5:0]"
    )
    api.send("POST", api.events, event("[7:0_"))
    assert (
        len(api.send("GET", "/tracks", params={"timerange": "[100:0_101:0)"}).json())
        == 1
    )
    assert api.send("GET", "/tracks", params={"timerange": "[2:0_3:0)"}).json() == []
    largest = "[281474976710655:999999998_281474976710655:999999999)"
    assert api.send("POST", api.events, event(largest)).json()["all_successful"]
    assert len(api.send("GET", api.events, params={"timerange": largest}).json()) == 2


def test_storage_errors_preserve_valid_batch_members(api) -> None:
    for text in ("bad\x00", "bad\ud800"):
        body = [event(), event(payload={"text": text}), event(id=123), event()]
        response = api.client.post(
            api.events,
            content=json.dumps(body),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200
        result = response.json()
        validate(result, contract_schema("track-event-post-responses"))
        assert [r["http_status_code"] for r in result["responses"]] == [
            201,
            400,
            400,
            201,
        ]
    assert len(api.send("GET", api.events).json()) == 4
