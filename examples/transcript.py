"""Create a transcript and fetch events overlapping its first two seconds."""

import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

base = os.getenv("TADOSS_URL", "http://127.0.0.1:8000").rstrip("/")
headers = {"Content-Type": "application/json"}
if token := os.getenv("TADOSS_TOKEN"):
    headers["Authorization"] = f"Bearer {token}"


def request(method, path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    with urlopen(
        Request(base + path, body, headers, method=method), timeout=10
    ) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


schema_id, track_id = str(uuid4()), str(uuid4())
request(
    "PUT",
    f"/schemas/{schema_id}",
    {
        "id": schema_id,
        "label": "Transcript payload",
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
            "additionalProperties": False,
        },
    },
)
request(
    "PUT",
    f"/tracks/{track_id}",
    {
        "id": track_id,
        "schema_id": schema_id,
        "label": "Example transcript",
        "timeline_reference": {
            "entity_id": os.getenv(
                "TAMS_ENTITY",
                "tams://tamoss.example.com/sources/8d7227aa-6bf2-4e84-881c-89eb046c3b48",
            )
        },
        "tags": {"language": "en"},
    },
)
result = request(
    "POST",
    f"/tracks/{track_id}/events",
    [
        {"timerange": "[0:0_1:500000000)", "payload": {"text": "Welcome to TADOSS."}},
        {
            "timerange": "[1:500000000_3:0)",
            "payload": {"text": "This transcript shares a media timeline."},
        },
    ],
)
if not result["all_successful"]:
    raise SystemExit(json.dumps(result, indent=2))
events = request(
    "GET", f"/tracks/{track_id}/events?" + urlencode({"timerange": "[0:0_2:0)"})
)
print(json.dumps({"track_id": track_id, "events": events}, indent=2))
