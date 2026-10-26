"""Repository documentation and deployment examples."""

import json
import re
from pathlib import Path

import yaml

from tests.support.chainsaw import validate_document

ROOT = Path(__file__).resolve().parents[1]


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
