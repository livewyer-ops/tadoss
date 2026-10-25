import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for
from mediatimestamp.immutable import TimeRange, Timestamp
from referencing import Registry
from referencing.exceptions import Unresolvable

from tadoss.domain.types import Document

SPEC = json.loads((Path(__file__).parent.parent / "contract/openapi.json").read_text())


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class APIError(Exception):
    def __init__(self, status: int, summary: str) -> None:
        self.status = status
        self.body = {
            "type": f"urn:tadoss:error:{status}",
            "summary": summary,
            "time": now(),
        }
        super().__init__(summary)


@lru_cache(maxsize=256)
def contract_validator(serialised: str) -> Draft202012Validator:
    """A validator for a contract fragment, keyed by its canonical JSON."""
    schema = json.loads(serialised)
    return Draft202012Validator({"components": SPEC["components"], **schema})


def validate(value: Any, schema: Document) -> None:
    validator = contract_validator(json.dumps(schema, sort_keys=True))
    try:
        validator.validate(value)
    except ValidationError as exc:
        location = "/".join(map(str, exc.absolute_path)) or "body"
        raise APIError(400, f"Invalid {location}: {exc.message}") from exc
    except RecursionError as exc:
        raise APIError(400, "JSON structure is too deeply nested") from exc


def json_equal(left: Any, right: Any) -> bool:
    """JSON equality distinguishes booleans from numbers, unlike Python equality."""
    return Draft202012Validator({"const": left}).is_valid(right)


def storage_safe(value: Any) -> None:
    """Reject strings PostgreSQL cannot represent before entering a transaction."""
    if isinstance(value, str):
        if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise APIError(
                400, "JSON strings must contain Unicode scalar values without NUL"
            )
    elif isinstance(value, dict):
        for key, item in value.items():
            storage_safe(key)
            storage_safe(item)
    elif isinstance(value, list):
        for item in value:
            storage_safe(item)


def contract_schema(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def payload_validator(schema: Document) -> Validator:
    return compiled_payload_validator(json.dumps(schema, sort_keys=True))


@lru_cache(maxsize=128)
def compiled_payload_validator(serialised: str) -> Validator:
    """Validator for a stored payload schema; rejected schemas are not cached."""
    schema = json.loads(serialised)
    # No remote schema retrieval: payload validation must not make network requests.
    dialect = schema.get("$schema", Draft202012Validator.META_SCHEMA["$id"])
    if not isinstance(dialect, str):
        raise APIError(400, "JSON Schema dialect must be a string URI")
    # None is a valid default at runtime although the stub types it more narrowly.
    validator_class = validator_for({"$schema": dialect}, default=None)  # pyright: ignore[reportArgumentType]
    if validator_class is None:
        raise APIError(400, "Unsupported JSON Schema dialect")
    try:
        validator_class.check_schema(schema)
        return validator_class(schema, registry=Registry())
    except SchemaError as exc:
        raise APIError(400, f"Invalid payload schema: {exc.message}") from exc
    except RecursionError as exc:
        raise APIError(400, "Payload schema is too deeply nested") from exc


def validate_payload(validator: Validator, payload: Any) -> None:
    try:
        validator.validate(payload)
    except (ValidationError, Unresolvable, RecursionError) as exc:
        raise APIError(
            400,
            "Payload does not match its schema, or a reference cannot be resolved",
        ) from exc


def time_range(value: Any, require_non_empty: bool = False) -> TimeRange:
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
        result = TimeRange.from_str(value)
    except ValueError as exc:
        raise APIError(400, "Invalid timerange") from exc
    if require_non_empty and result.is_empty():
        raise APIError(400, "Event timerange must not be empty")
    return result


def check_grid(timerange: TimeRange, track: Document) -> None:
    bounds = [bound for bound in (timerange.start, timerange.end) if bound is not None]
    check_grid_timestamps(bounds, track)


def check_grid_timestamps(timestamps: Iterable[Timestamp], track: Document) -> None:
    if rate := track.get("edit_rate"):
        for bound in timestamps:
            if bound.normalise(rate["numerator"], rate.get("denominator", 1)) != bound:
                raise APIError(
                    400, "Event timerange must align with the track's edit_rate"
                )


def coverage(events: Iterable[Document]) -> str:
    result = TimeRange.never()
    for event in events:
        result = result.extend_to_encompass_timerange(time_range(event["timerange"]))
    return str(result)


def writable(track: Document) -> None:
    if track["read_only"]:
        raise APIError(403, "Track is read-only")
