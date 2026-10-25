"""Validate wire responses without importing the runtime's validators."""

from fastapi.openapi.utils import get_openapi
from httpx import Response
from starlette.routing import compile_path
from tadoss.api.routes import router

from tests.support.bbc_contract import (
    bbc_spec,
    bbc_validator,
    response_contract,
    response_validator,
)

REGISTERED_PATHS = get_openapi(title="Routes", version="test", routes=router.routes)[
    "paths"
]
PATHS = [
    (path, compile_path(path)[0], item) for path, item in bbc_spec()["paths"].items()
]


def validate_response(response: Response) -> None:
    method = response.request.method.lower()
    for path, pattern, item in PATHS:
        if not pattern.fullmatch(response.request.url.path) or method not in item:
            continue
        response.read()
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
