"""Protocol checks executed in the qualified runtime image by Chainsaw."""

import os
import socket
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


def check_api():
    with httpx.Client(base_url="http://tadoss-api:8000", timeout=10) as client:
        assert client.get("/readyz").status_code == 200
        assert client.get("/tracks").status_code == 401

        def request(method, path, body=None, expected=200):
            response = client.request(
                method,
                path,
                params={"access_token": os.environ["TADOSS_API_TOKEN"]},
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
            {"id": schema, "schema": {"type": "object"}},
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
    print("Deployed authentication, event writes and worker deletion passed")


if __name__ == "__main__":
    try:
        check_database_tls()
        check_api()
    except httpx.HTTPError:
        raise SystemExit("HTTP operation failed") from None
