"""Protocol checks executed in the qualified runtime image."""

import argparse
import os
import socket
import ssl
import time
from uuid import uuid4

import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict
from tadoss.settings import Settings


def check_database_tls():
    uri = Settings.load().database_url()
    parameters = conninfo_to_dict(uri)
    assert parameters["sslmode"] == "verify-full"
    with psycopg.connect(uri, connect_timeout=5) as connection:
        assert connection.execute(
            "SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()"
        ).fetchone()[0]
    address = socket.getaddrinfo(
        parameters["host"], int(parameters.get("port", 5432)), type=socket.SOCK_STREAM
    )[0][4][0]
    try:
        psycopg.connect(
            uri, host="wrong-database.invalid", hostaddr=address, connect_timeout=5
        )
    except psycopg.OperationalError:
        pass
    else:
        raise AssertionError("Incorrect database hostname was accepted")
    print("Managed PostgreSQL TLS and hostname rejection passed")


def check_api(api_url="http://tadoss-api:8000", ca_file=None):
    tls = ssl.create_default_context(cafile=ca_file)
    with httpx.Client(
        base_url=api_url, verify=tls, timeout=10, trust_env=False
    ) as client:
        assert client.get("/readyz").status_code == 200
        assert client.get("/tracks").status_code == 401

        def request(method, path, body=None, expected=200, params=None):
            response = client.request(
                method,
                path,
                params={
                    **(params or {}),
                    "access_token": os.environ["TADOSS_API_TOKEN"],
                },
                json=body,
            )
            assert response.status_code == expected, (
                method,
                path,
                response.status_code,
            )
            assert response.headers.get("X-Request-ID")
            return response

        schema, track = str(uuid4()), str(uuid4())
        request(
            "PUT",
            "/schemas/" + schema,
            {
                "id": schema,
                "schema": {
                    "type": "object",
                    "required": ["index"],
                    "properties": {"index": {"type": "integer"}},
                },
            },
            201,
        )
        try:
            request(
                "PUT",
                "/tracks/" + track,
                {
                    "id": track,
                    "schema_id": schema,
                    "timeline_reference": {"entity_id": "urn:tadoss:qualification"},
                    "editable_events": True,
                },
                201,
            )
            path = "/tracks/" + track + "/events"
            payload = [
                {
                    "id": str(uuid4()),
                    "timerange": f"[{n}:0_{n + 1}:0)",
                    "payload": {"index": n},
                }
                for n in range(1001)
            ]
            for offset in range(0, len(payload), 500):
                result = request("POST", path, payload[offset : offset + 500]).json()
                assert all(
                    item["http_status_code"] == 201 for item in result["responses"]
                )
            extra = {
                "id": str(uuid4()),
                "timerange": "[1001:0_1002:0)",
                "payload": {"index": 1001},
            }
            mixed = request(
                "POST",
                path,
                [
                    payload[0],
                    extra,
                    {"timerange": "[0:0_1:0)", "payload": {"index": "invalid"}},
                ],
            ).json()
            assert not mixed["all_successful"]
            assert [row["http_status_code"] for row in mixed["responses"]] == [
                200,
                201,
                400,
            ]
            expected_ids = [row["id"] for row in [*payload, extra]]
            for reverse in (False, True):
                next_path = path
                params = {"limit": "127", "reverse_order": str(reverse).lower()}
                seen = []
                for _ in range(20):
                    page = request("GET", next_path, params=params)
                    head = request("HEAD", next_path, params=params)
                    assert head.content == b""
                    for name in (
                        "X-Paging-Limit",
                        "X-Paging-Count",
                        "X-Paging-Reverse-Order",
                        "X-Paging-Timerange",
                        "Link",
                        "X-Paging-NextKey",
                    ):
                        assert head.headers.get(name) == page.headers.get(name), name
                    assert int(page.headers["X-Paging-Count"]) == len(page.json())
                    assert (
                        page.headers["X-Paging-Reverse-Order"] == str(reverse).lower()
                    )
                    seen.extend(row["id"] for row in page.json())
                    if "next" not in page.links:
                        assert "X-Paging-NextKey" not in page.headers
                        break
                    next_path = page.links["next"]["url"]
                    assert "access_token" not in next_path
                    # httpx replaces the query when params is supplied.
                    link = httpx.URL(next_path)
                    params = dict(link.params)
                    next_path = link.path
                else:
                    raise AssertionError("Pagination did not terminate")
                assert seen == (
                    list(reversed(expected_ids)) if reverse else expected_ids
                )
            location = request("DELETE", path, expected=202).headers["Location"]
            # Deferred deletion exposes a status resource that must reach done.
            deadline = time.monotonic() + 120
            while (state := request("GET", location).json()["status"]) != "done":
                assert state != "error", "Worker reported a deletion failure"
                assert time.monotonic() < deadline, "Worker did not finish deletion"
                time.sleep(1)
            assert request("GET", path).json() == []
        finally:
            request("DELETE", "/tracks/" + track, expected=204)
            request("DELETE", "/schemas/" + schema, expected=204)
    print("Deployed authentication, batches, paging, HEAD and worker deletion passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-only", action="store_true")
    parser.add_argument("--api-url", default="http://tadoss-api:8000")
    parser.add_argument("--ca-file")
    arguments = parser.parse_args()
    try:
        if arguments.database_only:
            check_database_tls()
        else:
            check_api(arguments.api_url, arguments.ca_file)
    except httpx.HTTPError:
        raise SystemExit("HTTP operation failed") from None
