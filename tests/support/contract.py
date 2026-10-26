"""Validate wire responses without importing the runtime's validators."""

from fastapi.openapi.utils import get_openapi
from httpx import Response
from starlette.routing import compile_path
from tadoss.api.routes import router

from tests.support.bbc_contract import (
    bbc_spec,
    bbc_validator,
    header_validator,
    response_contract,
    response_validator,
)

REGISTERED_PATHS = get_openapi(title="Routes", version="test", routes=router.routes)[
    "paths"
]
PATHS = [
    (path, compile_path(path.replace("{name}", "{name:path}"))[0], item)
    for path, item in bbc_spec()["paths"].items()
]


def validate_headers(response: Response, path: str, method: str) -> None:
    definitions = response_contract(path, method, response.status_code).get(
        "headers", {}
    )
    for name, definition in definitions.items():
        # Continuation headers are absent on the last page.
        if name in {"Link", "X-Paging-NextKey"} and name not in response.headers:
            continue
        assert name in response.headers, f"Missing {name}"
        schema, validator = header_validator(
            definition, path, method, response.status_code, name
        )
        value = response.headers[name]
        if schema.get("type") == "integer":
            value = int(value)
        elif schema.get("type") == "boolean":
            assert value in {"true", "false"}
            value = value == "true"
        validator.validate(value)
    if "X-Paging-Count" in definitions:
        count = int(response.headers["X-Paging-Count"])
        assert 0 <= count <= int(response.headers["X-Paging-Limit"])
        if method == "get":
            assert count == len(response.json())
    if "Link" in definitions:
        assert ("next" in response.links) == ("X-Paging-NextKey" in response.headers)


def validate_response(response: Response) -> None:
    method = response.request.method.lower()
    for path, pattern, item in PATHS:
        if not pattern.fullmatch(response.request.url.path) or method not in item:
            continue
        response.read()
        if response.is_success:
            validate_headers(response, path, method)
        if method == "head" or response.status_code == 204:
            assert response.content == b""
            return
        if response.is_error:
            validator = bbc_validator("error.json")
        else:
            definition = response_contract(path, method, response.status_code)
            content = definition.get("content", {}).get("application/json")
            if content is None:
                return
            validator = response_validator(path, method, response.status_code)
        assert response.headers["content-type"].split(";")[0] == "application/json"
        validator.validate(response.json())
        return
