import base64
from urllib.parse import quote
from uuid import uuid4

import anyio
import httpx
from tadoss.api.http import resolve
from tadoss.domain.validation import (
    SPEC,
)

from tests.support.api import (
    PAYLOAD_SCHEMA,
    SCHEMA_ID,
    TRACK_ID,
    event,
)


def test_read_only_applies_to_all_mutations(api, subtests) -> None:
    api.send("POST", api.events, event())
    api.send("PUT", f"/tracks/{TRACK_ID}/read_only", True, status=204)
    for method, path, body in [
        ("PUT", f"/tracks/{TRACK_ID}", api.track),
        ("PUT", f"/tracks/{TRACK_ID}/label", "changed"),
        ("PUT", f"/tracks/{TRACK_ID}/tags/test", "changed"),
        ("DELETE", f"/tracks/{TRACK_ID}/tags/missing", None),
        ("DELETE", f"/tracks/{TRACK_ID}/label", None),
        ("PUT", f"/tracks/{TRACK_ID}/allow_overlaps", False),
        ("PUT", f"/tracks/{TRACK_ID}/editable_events", False),
        ("POST", api.events, event()),
        ("DELETE", api.events, None),
        ("DELETE", f"/tracks/{TRACK_ID}", None),
    ]:
        with subtests.test(path=path, method=method):
            api.send(method, path, body, status=403)
    api.send("PUT", f"/tracks/{TRACK_ID}/read_only", False, status=204)
    api.send("DELETE", api.events, status=204)


def test_encoded_tag_names_preserve_identity(api) -> None:
    # ASGITransport decodes the wire path once, matching Uvicorn.
    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api.app),
            base_url="http://test",
            headers=dict(api.client.headers),
        ) as client:
            for collection, identifier in [
                ("schemas", SCHEMA_ID),
                ("tracks", TRACK_ID),
            ]:
                root = f"/{collection}/{identifier}/tags"
                names = ["a/b", "a%2Fb", "check / & ✓", "question? hash# percent%"]
                for index, name in enumerate(names):
                    response = await client.put(
                        root + "/" + quote(name, safe=""), json=[str(index)]
                    )
                    assert response.status_code == 204, response.text
                for index, name in enumerate(names):
                    path = root + "/" + quote(name, safe="")
                    response = await client.get(path)
                    assert response.status_code == 200, response.text
                    assert response.json() == [str(index)]
                    head = await client.head(path)
                    assert head.status_code == 200 and head.content == b""
                assert (await client.get(root)).json() == dict(
                    zip(names, [[str(i)] for i in range(len(names))], strict=True)
                )
                for name in names:
                    path = root + "/" + quote(name, safe="")
                    assert (await client.delete(path)).status_code == 204
                    assert (await client.get(path)).status_code == 404

    anyio.run(exercise)


def test_schema_references_and_property_endpoints(api) -> None:
    schema = {
        "$defs": {"text": {"type": "string"}},
        "properties": {"text": {"$ref": "#/$defs/text"}},
    }
    sid = str(uuid4())
    api.send("PUT", f"/schemas/{sid}", {"id": sid, "schema": schema}, status=201)
    tid = str(uuid4())
    api.send(
        "PUT", f"/tracks/{tid}", {**api.track, "id": tid, "schema_id": sid}, status=201
    )
    assert api.send("POST", f"/tracks/{tid}/events", event()).json()["all_successful"]
    bad = str(uuid4())
    api.send("PUT", f"/schemas/{bad}", {"id": bad, "schema": {"type": 123}}, status=400)
    for collection, identifier, properties in [
        ("schemas", SCHEMA_ID, ["label", "description", "external_id"]),
        ("tracks", TRACK_ID, ["label", "description"]),
    ]:
        for prop in properties:
            path = f"/{collection}/{identifier}/{prop}"
            api.send("PUT", path, "value", status=204)
            assert api.send("GET", path).json() == "value"
            api.send("DELETE", path, status=204)
            api.send("GET", path, status=404)
        api.send("PUT", f"/{collection}/{identifier}/tags/a", ["b"], status=204)
        assert api.send("GET", f"/{collection}/{identifier}/tags/a").json() == ["b"]
        api.send("DELETE", f"/{collection}/{identifier}/tags/a", status=204)
    assert api.send("GET", f"/schemas/{SCHEMA_ID}/schema").json() == PAYLOAD_SCHEMA
    assert api.send("GET", "/track-delete-requests").json() == []


def test_schema_timestamps(api) -> None:
    sid = str(uuid4())
    api.send("PUT", f"/schemas/{sid}", {"id": sid, "schema": {}}, status=201)
    schema = api.send("GET", f"/schemas/{sid}").json()
    assert schema["created"] == schema["updated"]


def test_schema_listing_filters_and_paging(api, subtests) -> None:
    second = str(uuid4())
    api.send(
        "PUT",
        f"/schemas/{second}",
        {
            "id": second,
            "schema": {},
            "label": "Second",
            "external_id": "x-2",
            "tags": {"kind": ["b"]},
        },
        status=201,
    )
    assert len(api.send("GET", "/schemas").json()) == 2
    for params, count in [
        ({"label": "Second"}, 1),
        ({"external_id": "x-2"}, 1),
        ({"tag.kind": "b"}, 1),
        ({"tag_exists.kind": "false"}, 1),
        ({"tag.kind": "c"}, 0),
    ]:
        with subtests.test(params=params):
            assert len(api.send("GET", "/schemas", params=params).json()) == count
    first = api.send("GET", "/schemas", params={"limit": 1})
    assert first.headers["X-Paging-Limit"] == "1"
    assert first.headers["X-Paging-Count"] == "1"
    assert "X-Paging-NextKey" in first.headers
    rest = api.send("GET", first.headers["Link"].split(">", 1)[0][1:])
    assert len(rest.json()) == 1
    assert "Link" not in rest.headers
    capped = api.send("GET", "/schemas", params={"limit": 5000})
    assert capped.headers["X-Paging-Limit"] == "1000"
    negative = base64.urlsafe_b64encode(b"-1").decode()
    api.send("GET", "/schemas", params={"page": negative}, status=400)
    api.send("GET", "/schemas", params={"limit": "1", "limit ": "2"}, status=400)
    duplicate = api.client.get("/schemas?limit=1&limit=2")
    assert duplicate.status_code == 400
    head = api.send("HEAD", "/schemas")
    assert head.content == b""
    assert int(head.headers["Content-Length"]) == len(
        api.send("GET", "/schemas").content
    )
    assert (
        resolve({"$ref": "#/components/schemas/uuid"})
        == SPEC["components"]["schemas"]["uuid"]
    )
    assert resolve({"$ref": "#/paths/~1tracks/get"}) == SPEC["paths"]["/tracks"]["get"]


def test_read_only_tag_cannot_unlock(api) -> None:
    api.send("PUT", f"/tracks/{TRACK_ID}/tags/read_only", "tag", status=204)
    api.send("PUT", f"/tracks/{TRACK_ID}/read_only", True, status=204)
    api.send("PUT", f"/tracks/{TRACK_ID}/tags/read_only", "changed", status=403)
    api.send("DELETE", f"/tracks/{TRACK_ID}/tags/read_only", status=403)
    api.send("PUT", f"/tracks/{TRACK_ID}/read_only", False, status=204)


def test_json_equality_distinguishes_booleans(api) -> None:
    sid = str(uuid4())
    body = {"id": sid, "schema": {"const": True}}
    api.send("PUT", f"/schemas/{sid}", body, status=201)
    api.send("PUT", f"/schemas/{sid}", {**body, "schema": {"const": 1}}, status=403)
    original = api.send("GET", f"/tracks/{TRACK_ID}").json()
    api.send("PUT", f"/tracks/{TRACK_ID}", {**original, "extension": True}, status=204)
    before = api.send("GET", f"/tracks/{TRACK_ID}").json()
    api.send("PUT", f"/tracks/{TRACK_ID}", {**before, "extension": 1}, status=204)
    after = api.send("GET", f"/tracks/{TRACK_ID}").json()
    assert before["metadata_version"] != after["metadata_version"]


def test_metadata_edits_only_scan_changed_constraints(api, monkeypatch) -> None:
    from tadoss.adapters import postgres as store

    path = f"/tracks/{TRACK_ID}"
    api.send(
        "PUT",
        path,
        {**api.track, "allow_overlaps": False, "edit_rate": {"numerator": 25}},
        status=204,
    )
    api.send("POST", api.events, event("[0:0_0:40000000)"))
    before = api.send("GET", path).json()
    with monkeypatch.context() as patch:

        def unnecessary_scan(*_args):
            raise AssertionError("Unchanged constraints must not scan event history")

        patch.setattr(store, "track_bounds", unnecessary_scan)
        patch.setattr(store, "track_has_overlaps", unnecessary_scan)
        api.send("PUT", path + "/label", "renamed", status=204)
        api.send("PUT", path + "/tags/language", "en", status=204)
        api.send("PUT", path + "/editable_events", False, status=204)
        current = api.send("GET", path).json()
        api.send("PUT", path, {**current, "description": "Updated"}, status=204)
        api.send("PUT", path + "/read_only", True, status=204)
        api.send("PUT", path + "/read_only", False, status=204)
    after = api.send("GET", path).json()
    assert after["events_updated"] == before["events_updated"]
    assert after["metadata_version"] != before["metadata_version"]
    assert api.send("GET", api.events).json()[0]["timerange"] == "[0:0_0:40000000)"
    api.send("PUT", path, {**after, "edit_rate": {"numerator": 24}}, status=400)
    api.send("PUT", path + "/allow_overlaps", True, status=204)
    api.send("POST", api.events, event("[0:0_0:40000000)"))
    api.send("PUT", path + "/allow_overlaps", False, status=400)


def test_collection_filters_preserve_scalar_array_and_or_membership(
    api, subtests
) -> None:
    from urllib.parse import quote

    key = 'language.part/"'
    for collection, base in (("schemas", {"schema": {}}), ("tracks", api.track)):
        identifiers = [str(uuid4()) for _ in range(4)]
        for identifier, tags in zip(
            identifiers, ({key: "fr"}, {key: ["en", "fr"]}, {key: []}, {}), strict=True
        ):
            api.send(
                "PUT",
                f"/{collection}/{identifier}",
                {**base, "id": identifier, "label": "match", "tags": tags},
                status=201,
            )
        for options, expected in [
            ({f"tag.{key}": "absent,fr"}, identifiers[:2]),
            ({f"tag.{key}": "en"}, identifiers[1:2]),
            ({f"tag.{key}": "f"}, []),
            ({f"tag_exists.{key}": "true"}, identifiers[:3]),
            ({f"tag_exists.{key}": "false"}, identifiers[3:]),
        ]:
            with subtests.test(collection=collection, options=options):
                response = api.send(
                    "GET",
                    f"/{collection}",
                    params={"label": "match", "limit": 1, **options},
                )
                found = [row["id"] for row in response.json()]
                while "Link" in response.headers:
                    response = api.send(
                        "GET", response.headers["Link"].split(">", 1)[0][1:]
                    )
                    found.extend(row["id"] for row in response.json())
                assert found == sorted(expected)
    identifier = str(uuid4())
    entity = "urn:source:part,with,commas"
    api.send(
        "PUT",
        f"/tracks/{identifier}",
        {
            **api.track,
            "id": identifier,
            "timeline_reference": {"entity_id": entity},
            "editable_events": False,
        },
        status=201,
    )
    options = {
        "schema_id": SCHEMA_ID,
        "editable_events": "false",
        "timeline_reference.entity_id": "urn:absent," + quote(entity, safe=""),
    }
    assert [row["id"] for row in api.send("GET", "/tracks", params=options).json()] == [
        identifier
    ]
