# ruff: noqa: S603, S607
"""Pinned contract, repository documentation and deployment examples."""

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from httpx import Request, Response
from jsonschema import ValidationError
from tadoss.api import create_app, published_contract
from tadoss.domain.validation import SPEC
from tadoss.settings import Settings

from tests.support.chainsaw import validate_document
from tests.support.contract import REGISTERED_PATHS, validate_response
from tests.test_vendor_spec import load_vendor_spec

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def contract_client():
    # No lifespan or pool: every published operation must authenticate first.
    app = create_app(
        settings=Settings(database="postgresql://unused", api_token="test-only")
    )
    client = TestClient(app)
    client.event_hooks["response"].append(validate_response)
    yield client
    client.close()


def test_pinned_contract_bundle(tmp_path):
    source = ROOT / "src/vendor/bbc-tads"
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    assert revision == SPEC["x-upstream-revision"]
    assert load_vendor_spec().bundle(source, tmp_path, revision) == SPEC
    assert set(REGISTERED_PATHS) == set(SPEC["paths"])


@pytest.mark.parametrize(
    "path,method",
    [
        (path, method)
        for path, operations in SPEC["paths"].items()
        for method in operations
        if method != "parameters"
    ],
)
def test_published_operation(path, method, contract_client):
    assert method in REGISTERED_PATHS[path]
    assert published_contract()["paths"][path][method] == SPEC["paths"][path][method]
    concrete = re.sub(r"\{[^}]+\}", "00000000-0000-4000-8000-000000000000", path)
    assert contract_client.request(method, concrete).status_code == 401


def test_documentation_links():
    for path in [
        *ROOT.glob("*.md"),
        ROOT / "src/README.md",
        ROOT / "operator/README.md",
        ROOT / "deploy/README.md",
        *ROOT.glob("docs/**/*.md"),
    ]:
        content = path.read_text()
        for target in re.findall(r"\]\(([^)]+)\)", content):
            if "://" in target or target.startswith(("#", "mailto:")):
                continue
            assert (path.parent / target.split("#")[0]).exists(), (path, target)


def test_profile_versions():
    versions = {
        item["version"]
        for item in json.loads(
            (ROOT / "operator/internal/releases/catalogue.json").read_text()
        )
    }
    for profile in yaml.safe_load((ROOT / "deploy/profiles.yaml").read_text()):
        directory = ROOT / "deploy/environments" / profile
        assert (
            yaml.safe_load((directory / "operator/defaults.yaml").read_text())[
                "profile"
            ]
            == profile
        )
        for path in (directory / "instances").glob("*/tadoss.yaml"):
            assert yaml.safe_load(path.read_text())["spec"]["version"] in versions


def test_chainsaw_uses_native_operations_and_python_sources():
    for directory in (ROOT / "operator/test/chainsaw", ROOT / "tests/deployed"):
        for path in directory.rglob("*.yaml"):
            for document in yaml.safe_load_all(path.read_text()):
                try:
                    validate_document(document, path)
                except AssertionError as exc:
                    raise AssertionError(f"{path}: {exc}") from exc


def test_response_contract_validator_rejects_malformed_success():
    response = Response(
        200, json=[{"id": "invalid"}], request=Request("GET", "http://test/tracks")
    )
    with pytest.raises(ValidationError):
        validate_response(response)
    response = Response(
        204,
        content=b"unexpected",
        request=Request(
            "DELETE", "http://test/tracks/00000000-0000-4000-8000-000000000000"
        ),
    )
    with pytest.raises(AssertionError):
        validate_response(response)


def test_response_validation_is_independent_of_the_generated_bundle(monkeypatch):
    import copy
    import importlib

    from jsonschema import Draft202012Validator

    from tests.support import contract

    broken = copy.deepcopy(SPEC)
    broken["components"]["schemas"]["uuid"] = {"type": "string"}
    track = {
        "id": "invalid",
        "schema_id": "00000000-0000-4000-8000-000000000000",
        "timeline_reference": {"entity_id": "urn:test:timeline"},
    }
    Draft202012Validator(
        {"components": broken["components"], "$ref": "#/components/schemas/track"}
    ).validate(track)
    response = Response(200, json=[track], request=Request("GET", "http://test/tracks"))
    read_text = Path.read_text

    def read_bundle(path, *args, **kwargs):
        if path.resolve() == ROOT / "src/app/tadoss/contract/openapi.json":
            return json.dumps(broken)
        return read_text(path, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_text", read_bundle)
            importlib.reload(contract)
            with pytest.raises(ValidationError, match="invalid"):
                contract.validate_response(response)
    finally:
        importlib.reload(contract)
