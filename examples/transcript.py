"""Create a transcript and fetch events overlapping its first two seconds."""

import json
import os
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

base = os.getenv("TADOSS_URL", "http://127.0.0.1:8000").rstrip("/")
if not base.startswith(("http://", "https://")):
    raise SystemExit("TADOSS_URL must be an http or https URL")
headers = {"Content-Type": "application/json"}
token = os.getenv("TADOSS_API_TOKEN")


def request(method, path, data=None):
    if token:
        path += ("&" if "?" in path else "?") + urlencode({"access_token": token})
    body = json.dumps(data).encode() if data is not None else None
    # The scheme is checked above, which is what the audit rule asks for.
    call = Request(base + path, body, headers, method=method)  # noqa: S310
    try:
        with urlopen(call, timeout=10) as response:  # noqa: S310
            raw = response.read()
    except HTTPError as error:
        detail = error.read().decode(errors="replace")
        print(f"{method} request failed with {error.code}: {detail}", file=sys.stderr)
        raise SystemExit(1) from error
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
