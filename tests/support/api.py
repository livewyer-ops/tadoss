import copy
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from tadoss.api import create_app as runtime_app
from tadoss.settings import Settings

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


def create_app(database=None, token=None, dev=None):
    settings = Settings.load(
        database=database, api_token=token, basic_auth_password=token, dev=dev
    )
    return runtime_app(settings=settings)


def event(timerange="[0:0_1:0)", **fields):
    return {"timerange": timerange, "payload": {"text": "A transcript"}, **fields}


@dataclass
class API:
    database: str
    app: FastAPI
    client: TestClient
    schema: dict[str, Any]
    track: dict[str, Any]
    events: str = f"/tracks/{TRACK_ID}/events"

    def send(self, method, path, body=None, status=200, **kwargs) -> Response:
        if body is not None:
            kwargs["json"] = body
        response = self.client.request(method, path, **kwargs)
        assert response.status_code == status, response.text
        return response

    def register(self, **extra):
        from tests.support.bbc_contract import bbc_spec

        return self.send(
            "POST",
            "/service/webhooks",
            {
                "url": "https://receiver.example/events",
                "events": list(bbc_spec()["webhooks"]),
                "api_key_name": "X-Token",
                "api_key_value": "secret",
                **extra,
            },
            status=201,
        ).json()


def records():
    return (
        {"id": SCHEMA_ID, "schema": copy.deepcopy(PAYLOAD_SCHEMA)},
        {
            "id": TRACK_ID,
            "schema_id": SCHEMA_ID,
            "timeline_reference": {"entity_id": ENTITY},
        },
    )
