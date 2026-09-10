import json
import re
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from jsonschema.validators import validator_for
from mediatimestamp.immutable import TimeRange, Timestamp
from referencing import Registry
from referencing.exceptions import Unresolvable

SPEC = json.loads((Path(__file__).parent / "contract/openapi.json").read_text())


def now():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class APIError(Exception):
    def __init__(self, status, summary):
        self.status = status
        self.body = {
            "type": f"urn:tadoss:error:{status}",
            "summary": summary,
            "time": now(),
        }
        super().__init__(summary)


def validate(value, schema):
    validator = Draft202012Validator({"components": SPEC["components"], **schema})
    try:
        validator.validate(value)
    except ValidationError as exc:
        location = "/".join(map(str, exc.absolute_path)) or "body"
        raise APIError(400, f"Invalid {location}: {exc.message}") from exc
    except RecursionError as exc:
        raise APIError(400, "JSON structure is too deeply nested") from exc


def contract_schema(name):
    return {"$ref": f"#/components/schemas/{name}"}


def payload_validator(schema):
    # No remote schema retrieval: payload validation must not make network requests.
    dialect = schema.get("$schema", Draft202012Validator.META_SCHEMA["$id"])
    if not isinstance(dialect, str):
        raise APIError(400, "JSON Schema dialect must be a string URI")
    validator_class = validator_for({"$schema": dialect}, default=None)
    if validator_class is None:
        raise APIError(400, "Unsupported JSON Schema dialect")
    try:
        validator_class.check_schema(schema)
        return validator_class(schema, registry=Registry())
    except SchemaError as exc:
        raise APIError(400, f"Invalid payload schema: {exc.message}") from exc
    except RecursionError as exc:
        raise APIError(400, "Payload schema is too deeply nested") from exc


def validate_payload(validator, payload):
    try:
        validator.validate(payload)
    except (ValidationError, Unresolvable, RecursionError) as exc:
        raise APIError(
            400,
            "Payload does not match its schema, or a reference cannot be resolved",
        ) from exc


def time_range(value):
    pattern = SPEC["components"]["schemas"]["timerange"]["pattern"]
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise APIError(
            400, "Invalid timerange; use seconds:nanoseconds and boundary markers"
        )
    if (
        "_" not in value
        and ":" in value
        and (value.startswith("(") or value.endswith(")"))
    ):
        raise APIError(400, "Instantaneous timeranges must be inclusive")
    for timestamp in re.findall(r"-?\d+:\d+", value):
        seconds, _ = timestamp.lstrip("-").split(":")
        if len(seconds) > 15 or int(seconds) >= Timestamp.MAX_SECONDS:
            raise APIError(
                400,
                "Timestamp exceeds the BBC timestamp library's 48-bit seconds limit",
            )
    try:
        return TimeRange.from_str(value)
    except ValueError as exc:
        raise APIError(400, "Invalid timerange") from exc


def check_grid(timerange, track):
    if rate := track.get("edit_rate"):
        for bound in (timerange.start, timerange.end):
            if (
                bound is not None
                and bound.normalise(rate["numerator"], rate.get("denominator", 1))
                != bound
            ):
                raise APIError(
                    400, "Event timerange must align with the track's edit_rate"
                )


def event_key(event, reverse=False):
    timerange = time_range(event["timerange"])
    start = (
        timerange.start.to_nanosec() if timerange.start is not None else float("-inf"),
        not timerange.includes_start(),
    )
    end = (
        timerange.end.to_nanosec() if timerange.end is not None else float("inf"),
        timerange.includes_end(),
    )
    return (*end, *start, event["id"]) if reverse else (*start, *end, event["id"])


def coverage(events):
    result = TimeRange.never()
    for event in events:
        result = result.extend_to_encompass_timerange(time_range(event["timerange"]))
    return str(result)
