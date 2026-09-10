import copy
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from uuid import uuid4

from fastapi.testclient import TestClient
from psycopg import connect, sql
from psycopg.conninfo import make_conninfo

from tadoss.api import create_app
from tadoss.store import DEFAULT_DATABASE
from tadoss.validation import SPEC, contract_schema, validate

SCHEMA_ID = "adb87970-b03a-4266-b5d8-f3f6541fb73a"
TRACK_ID = "20992138-abdb-4daf-84f3-a0e07dabb5ca"
ENTITY = "tams://tamoss.example.com/sources/8d7227aa-6bf2-4e84-881c-89eb046c3b48"
PAYLOAD_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["text"],
    "properties": {"text": {"type": "string"}},
    "additionalProperties": False,
}


def event(timerange="[0:0_1:0)", **fields):
    return {"timerange": timerange, "payload": {"text": "A transcript"}, **fields}


class StoreTests(unittest.TestCase):
    def setUp(self):
        database = os.getenv("TADOSS_TEST_DATABASE", DEFAULT_DATABASE)
        schema = "test_" + uuid4().hex
        with connect(database) as connection:
            connection.execute(
                sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema))
            )

        def cleanup():
            with connect(database) as connection:
                connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )

        self.addCleanup(cleanup)
        self.database = make_conninfo(database, options=f"-c search_path={schema}")
        self.app = create_app(self.database, token="test-secret")
        self.client = self.enterContext(TestClient(self.app))
        self.client.headers["Authorization"] = "Bearer test-secret"
        self.schema = {"id": SCHEMA_ID, "schema": copy.deepcopy(PAYLOAD_SCHEMA)}
        self.track = {
            "id": TRACK_ID,
            "schema_id": SCHEMA_ID,
            "timeline_reference": {"entity_id": ENTITY},
        }
        self.send("PUT", f"/schemas/{SCHEMA_ID}", self.schema, status=201)
        self.send("PUT", f"/tracks/{TRACK_ID}", self.track, status=201)
        self.events = f"/tracks/{TRACK_ID}/events"

    def send(self, method, path, body=None, status=200, **kwargs):
        if body is not None:
            kwargs["json"] = body
        response = self.client.request(method, path, **kwargs)
        self.assertEqual(response.status_code, status, response.text)
        if status >= 400:
            validate(response.json(), contract_schema("error"))
        return response

    def test_lifecycle_filters_and_persistence(self):
        validate(self.send("GET", "/service").json(), contract_schema("service"))
        created = self.send("GET", f"/tracks/{TRACK_ID}").json()
        self.send("POST", self.events, event())
        after_events = self.send("GET", f"/tracks/{TRACK_ID}").json()
        self.assertEqual(created["metadata_version"], after_events["metadata_version"])
        self.assertNotEqual(created["events_updated"], after_events["events_updated"])
        self.send("PUT", f"/tracks/{TRACK_ID}/tags/language", ["en", "fr"], status=204)
        self.send("PUT", f"/tracks/{TRACK_ID}/label", "Transcript", status=204)
        params = {
            "timeline_reference.entity_id": quote(ENTITY, safe=""),
            "tag.language": "fr",
            "tag_exists.language": "true",
            "label": "Transcript",
        }
        self.assertEqual(len(self.send("GET", "/tracks", params=params).json()), 1)
        self.assertEqual(
            self.send("GET", "/tracks", params={"tag.language": "f"}).json(), []
        )
        self.assertEqual(
            self.send("GET", "/tracks", params={"tag_exists.language": "false"}).json(),
            [],
        )
        changed = self.send(
            "GET", f"/tracks/{TRACK_ID}", params={"include_timerange": "true"}
        ).json()
        self.assertEqual(changed["timerange"], "[0:0_1:0)")
        self.assertNotEqual(created["metadata_version"], changed["metadata_version"])
        self.send("PUT", f"/tracks/{TRACK_ID}", changed, status=204)
        self.assertEqual(
            self.send("GET", f"/tracks/{TRACK_ID}").json()["metadata_version"],
            changed["metadata_version"],
        )
        with TestClient(create_app(self.database, token="test-secret")) as restarted:
            self.assertEqual(
                len(
                    restarted.get(
                        self.events, headers={"Authorization": "Bearer test-secret"}
                    ).json()
                ),
                1,
            )
        self.send("DELETE", f"/schemas/{SCHEMA_ID}", status=400)
        self.send(
            "PUT", f"/schemas/{SCHEMA_ID}", {**self.schema, "schema": {}}, status=403
        )
        self.send("DELETE", f"/tracks/{TRACK_ID}", status=204)
        self.send("GET", self.events, status=404)
        self.send("DELETE", f"/schemas/{SCHEMA_ID}", status=204)

    def test_bulk_validation_editing_and_global_ids(self):
        identifier = str(uuid4())
        response = self.send(
            "POST",
            self.events,
            [
                event(id=identifier),
                event(payload={"text": 5}),
                {"payload": {}},
                event("[1:0_2:0)"),
            ],
        ).json()
        self.assertFalse(response["all_successful"])
        self.assertEqual(
            [r["http_status_code"] for r in response["responses"]], [201, 400, 400, 201]
        )
        validate(response, contract_schema("track-event-post-responses"))
        edited = self.send(
            "POST", self.events, event(id=identifier, payload={"text": "Corrected"})
        ).json()
        self.assertEqual(edited["responses"][0]["http_status_code"], 200)
        other = str(uuid4())
        self.send("PUT", f"/tracks/{other}", {**self.track, "id": other}, status=201)
        duplicate = self.send(
            "POST", f"/tracks/{other}/events", event(id=identifier)
        ).json()
        self.assertEqual(duplicate["responses"][0]["http_status_code"], 400)
        self.send("PUT", f"/tracks/{TRACK_ID}/editable_events", False, status=204)
        immutable = self.send("POST", self.events, event(id=identifier)).json()
        self.assertEqual(immutable["responses"][0]["http_status_code"], 403)
        self.send("DELETE", self.events, params={"ids": identifier}, status=204)
        self.assertEqual(
            self.send("POST", self.events, event(id=identifier)).json()["responses"][0][
                "http_status_code"
            ],
            201,
        )

    def test_read_only_applies_to_all_mutations(self):
        self.send("POST", self.events, event())
        self.send("PUT", f"/tracks/{TRACK_ID}/read_only", True, status=204)
        for method, path, body in [
            ("PUT", f"/tracks/{TRACK_ID}", self.track),
            ("PUT", f"/tracks/{TRACK_ID}/label", "changed"),
            ("PUT", f"/tracks/{TRACK_ID}/tags/test", "changed"),
            ("PUT", f"/tracks/{TRACK_ID}/allow_overlaps", False),
            ("PUT", f"/tracks/{TRACK_ID}/editable_events", False),
            ("POST", self.events, event()),
            ("DELETE", self.events, None),
            ("DELETE", f"/tracks/{TRACK_ID}", None),
        ]:
            with self.subTest(path=path, method=method):
                self.send(method, path, body, status=403)
        self.send("PUT", f"/tracks/{TRACK_ID}/read_only", False, status=204)
        self.send("DELETE", self.events, status=204)

    def test_boundaries_sorting_pagination_and_contained_deletion(self):
        inputs = [
            event("[2:0_20:0)"),
            event("[0:0_10:0)"),
            event("[1:0_2:0)"),
            event("[1:0]"),
            event("[-0:1_0:0)"),
            event("[10000000000:1_10000000000:2)"),
        ]
        self.send("POST", self.events, inputs)
        reverse = self.send("GET", self.events, params={"reverse_order": "true"}).json()
        self.assertEqual(
            [e["timerange"] for e in reverse],
            [inputs[i]["timerange"] for i in [5, 0, 1, 2, 3, 4]],
        )
        self.assertEqual(
            len(self.send("GET", self.events, params={"timerange": "[1:0]"}).json()), 3
        )
        first = self.send("GET", self.events, params={"limit": 2})
        all_events = first.json()
        while "Link" in first.headers:
            first = self.send("GET", first.headers["Link"].split(">", 1)[0][1:])
            all_events.extend(first.json())
        self.assertEqual(len({e["id"] for e in all_events}), 6)
        head = self.send("HEAD", self.events, params={"limit": 2})
        self.assertEqual(head.content, b"")
        self.assertEqual(head.headers["X-Paging-Count"], "2")
        self.send("DELETE", self.events, params={"timerange": "[1:0_2:0)"}, status=204)
        self.assertEqual(len(self.send("GET", self.events).json()), 4)
        self.assertEqual(
            len(
                self.send("GET", "/tracks", params={"timerange": "[11:0_12:0)"}).json()
            ),
            1,
        )
        self.assertEqual(
            self.send("GET", "/tracks", params={"timerange": "[21:0_22:0)"}).json(), []
        )
        for timerange in ["_", "()", "_0:0]", "(0:0_", "[0:0_0:0)"]:
            self.send("GET", self.events, params={"timerange": timerange})

    def test_overlap_grid_and_atomic_concurrent_writes(self):
        self.send(
            "PUT",
            f"/tracks/{TRACK_ID}",
            {**self.track, "allow_overlaps": False, "edit_rate": {"numerator": 25}},
            status=204,
        )
        response = self.send(
            "POST",
            self.events,
            [
                event("[0:0_1:0)"),
                event("[1:0_2:0)"),
                event("[0:1_0:2)"),
                event("[1:0]"),
            ],
        ).json()
        self.assertEqual(
            [r["http_status_code"] for r in response["responses"]], [201, 201, 400, 400]
        )
        self.send("DELETE", self.events, status=204)

        def write(_):
            return self.client.post(self.events, json=event()).json()["responses"][0][
                "http_status_code"
            ]

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sorted(pool.map(write, range(4))), [201, 400, 400, 400])
        self.send("PUT", f"/tracks/{TRACK_ID}/allow_overlaps", True, status=204)
        self.send("POST", self.events, event())
        self.send("PUT", f"/tracks/{TRACK_ID}/allow_overlaps", False, status=400)
        self.send("DELETE", self.events, status=204)
        self.send(
            "PUT",
            f"/tracks/{TRACK_ID}",
            {**self.track, "edit_rate": {"numerator": 30000, "denominator": 1001}},
            status=204,
        )
        aligned = self.send("POST", self.events, event("[0:0_0:33366666)")).json()
        self.assertTrue(aligned["all_successful"])

    def test_request_boundaries_and_auth(self):
        for value in [
            "not-a-range",
            "[01:0_2:0)",
            "[0:1000000000]",
            "(1:0)",
            "[281474976710656:0]",
            "[0:0]\n",
        ]:
            self.send("GET", self.events, params={"timerange": value}, status=400)
        for params in [
            {"limit": 0},
            {"page": "garbage"},
            {"reverse_order": "yes"},
            {"ids": "invalid"},
            {"unexpected": "1"},
        ]:
            self.send("GET", self.events, params=params, status=400)
        self.send("GET", "/tracks/not-a-uuid", status=400)
        self.send("POST", self.events, [], status=400)
        self.send(
            "PUT", f"/tracks/{TRACK_ID}", {**self.track, "id": str(uuid4())}, status=400
        )
        for raw in ['{"payload":NaN}', '{"payload":1e999}', "{bad json"]:
            self.send(
                "POST",
                self.events,
                content=raw,
                headers={"Content-Type": "application/json"},
                status=400,
            )
        self.send(
            "POST",
            self.events,
            content="x" * (1024 * 1024 + 1),
            headers={"Content-Type": "application/json"},
            status=413,
        )
        self.send("POST", self.events, content="{}", status=415)
        self.send(
            "GET", "/tracks", headers={"Authorization": "Bearer wrong"}, status=401
        )
        self.send("GET", "/tracks", headers={"Authorization": ""}, status=401)
        self.send(
            "GET",
            "/tracks",
            headers={"Authorization": ""},
            params={"access_token": "test-secret"},
        )

    def test_schema_references_and_property_endpoints(self):
        schema = {
            "$defs": {"text": {"type": "string"}},
            "properties": {"text": {"$ref": "#/$defs/text"}},
        }
        sid = str(uuid4())
        self.send("PUT", f"/schemas/{sid}", {"id": sid, "schema": schema}, status=201)
        tid = str(uuid4())
        self.send(
            "PUT",
            f"/tracks/{tid}",
            {**self.track, "id": tid, "schema_id": sid},
            status=201,
        )
        self.assertTrue(
            self.send("POST", f"/tracks/{tid}/events", event()).json()["all_successful"]
        )
        bad = str(uuid4())
        self.send(
            "PUT", f"/schemas/{bad}", {"id": bad, "schema": {"type": 123}}, status=400
        )
        for collection, identifier, properties in [
            ("schemas", SCHEMA_ID, ["label", "description", "external_id"]),
            ("tracks", TRACK_ID, ["label", "description"]),
        ]:
            for prop in properties:
                path = f"/{collection}/{identifier}/{prop}"
                self.send("PUT", path, "value", status=204)
                self.assertEqual(self.send("GET", path).json(), "value")
                self.send("DELETE", path, status=204)
                self.send("GET", path, status=404)
            self.send("PUT", f"/{collection}/{identifier}/tags/a", ["b"], status=204)
            self.assertEqual(
                self.send("GET", f"/{collection}/{identifier}/tags/a").json(), ["b"]
            )
            self.send("DELETE", f"/{collection}/{identifier}/tags/a", status=204)
        self.assertEqual(
            self.send("GET", f"/schemas/{SCHEMA_ID}/schema").json(), PAYLOAD_SCHEMA
        )
        self.assertEqual(self.send("GET", "/track-delete-requests").json(), [])

    def test_published_contract_matches_routes(self):
        contract = self.send("GET", "/openapi.json").json()
        self.assertEqual(contract["x-upstream-revision"], SPEC["x-upstream-revision"])
        self.assertNotIn("/service/webhooks", contract["paths"])
        for path, operations in contract["paths"].items():
            methods = {
                method.upper() for method in operations if method != "parameters"
            }
            self.assertTrue(
                any(
                    route.path == path and methods <= route.methods
                    for route in self.app.routes
                )
            )

        # Verify all bundled references resolve without external files or network.
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


if __name__ == "__main__":
    unittest.main()
