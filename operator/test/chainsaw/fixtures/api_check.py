# ruff: noqa: S310
"""HTTP checks against the fixture service configured by the Chainsaw Job."""

import json
import os
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

base = os.getenv("API_URL", "http://journey-api:8000")
token = os.environ["TADOSS_TOKEN"]
deadline = time.monotonic() + float(os.getenv("ROLLOUT_TIMEOUT", "240"))


def retry(action):
    # Service endpoints and rollouts settle after the Deployment reports available.
    while True:
        try:
            return action()
        except (URLError, ConnectionResetError, TimeoutError, AssertionError) as error:
            if isinstance(error, HTTPError) and error.code < 500:
                raise
            if time.monotonic() > deadline:
                raise
            time.sleep(2)


def status(path, headers):
    try:
        with urlopen(Request(base + path, headers=headers), timeout=10) as response:
            return response.status
    except HTTPError as error:
        return error.code


def request(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    path += "?" + urlencode({"access_token": token})

    def call():
        with urlopen(
            Request(base + path, data, headers, method=method), timeout=10
        ) as response:
            payload = response.read()
            return json.loads(payload) if payload else None

    return retry(call)


def main():
    if os.environ["MODE"] == "auth":

        def rotated():
            fresh = status("/tracks?" + urlencode({"access_token": token}), {})
            stale = status(
                "/tracks?" + urlencode({"access_token": os.environ["OLD_TOKEN"]}), {}
            )
            assert (fresh, stale) == (200, 401), (
                f"rotation still in progress: new={fresh} old={stale}"
            )

        retry(rotated)
        print("Rotated token accepted, old token rejected")
        return
    schema = "adb87970-b03a-4266-b5d8-f3f6541fb73a"
    track = "20992138-abdb-4daf-84f3-a0e07dabb5ca"
    event = "6cfeb3b9-fb8a-41a1-a36f-275848d05c30"
    if os.environ["MODE"] == "seed":
        request(
            "PUT",
            "/schemas/" + schema,
            {
                "id": schema,
                "schema": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {"text": {"type": "string"}},
                },
            },
        )
        request(
            "PUT",
            "/tracks/" + track,
            {
                "id": track,
                "schema_id": schema,
                "timeline_reference": {
                    "entity_id": "tams://tamoss.example.com/sources/" + track
                },
                "allow_overlaps": False,
            },
        )
        result = request(
            "POST",
            "/tracks/" + track + "/events",
            [
                {
                    "id": event,
                    "timerange": "[10000000000:1_10000000000:2)",
                    "payload": {"text": "Persisted"},
                },
                {"timerange": "[0:0_1:0)", "payload": {"text": 42}},
            ],
        )
        assert [r["http_status_code"] for r in result["responses"]] == [201, 400], (
            result
        )
    events = request("GET", "/tracks/" + track + "/events")
    assert len(events) == 1 and events[0]["payload"]["text"] == "Persisted", events
    assert events[0]["timerange"] == "[10000000000:1_10000000000:2)", events
    assert retry(lambda: status("/tracks", {})) == 401, (
        "Unauthenticated access was accepted"
    )
    print("API validation and persistence passed")


if __name__ == "__main__":
    try:
        main()
    except HTTPError as error:
        raise SystemExit(f"HTTP operation failed with status {error.code}") from None
    except (HTTPException, OSError, ValueError):
        raise SystemExit("HTTP operation failed") from None
