import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release", ROOT / "scripts/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_release_rejects_catalogue_schema_drift(tmp_path, monkeypatch):
    catalogue = tmp_path / "operator/internal/releases/catalogue.json"
    catalogue.parent.mkdir(parents=True)
    catalogue.write_text(
        json.dumps([{"version": release.RELEASE["version"], "schemaTarget": "wrong"}])
    )
    monkeypatch.setattr(release, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="catalogue"):
        release.check()


def test_release_metadata_matches_runtime():
    release.check()


def test_preflight_requires_explicit_not_found(monkeypatch):
    from unittest.mock import Mock
    from urllib.error import HTTPError
    from urllib.request import Request

    definition = importlib.util.spec_from_file_location(
        "preflight", ROOT / "scripts/release_preflight.py"
    )
    preflight = importlib.util.module_from_spec(definition)
    definition.loader.exec_module(preflight)
    request = Request("https://registry-1.docker.io/v2/test/manifests/version")
    for code in (401, 403, 429, 500):
        monkeypatch.setattr(
            preflight,
            "urlopen",
            Mock(side_effect=HTTPError(request.full_url, code, "failure", None, None)),
        )
        with pytest.raises(HTTPError):
            preflight.absent(request)
    monkeypatch.setattr(
        preflight,
        "urlopen",
        Mock(side_effect=HTTPError(request.full_url, 404, "absent", None, None)),
    )
    preflight.absent(request)


def test_artifacts_bind_images_and_have_valid_checksums(tmp_path, monkeypatch):
    import hashlib

    import yaml

    runtime = "livewyer/tadoss-api@sha256:" + "a" * 64
    operator = "livewyer/tadoss-operator@sha256:" + "b" * 64
    catalogue = tmp_path / "operator/internal/releases/catalogue.json"
    catalogue.parent.mkdir(parents=True)
    catalogue.write_text(
        json.dumps(
            [
                {
                    "version": release.RELEASE["version"],
                    "apiImage": runtime,
                    "schemaTarget": release.RELEASE["schemaTarget"],
                }
            ]
        )
    )
    install = tmp_path / "deploy/operator/install.yaml"
    install.parent.mkdir(parents=True)
    install.write_text((ROOT / "deploy/operator/install.yaml").read_text())
    for relative in (
        "deploy/platform/helmfile.yaml.gotmpl",
        "deploy/kind/single-node.yaml",
        "deploy/profiles.yaml",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / relative).read_text())
    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(
        release.subprocess, "check_output", lambda *_args, **_kw: "c" * 40
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "release",
            "artifacts",
            "--runtime-image",
            runtime,
            "--operator-image",
            operator,
            "--output",
            str(tmp_path / "out"),
        ],
    )
    release.main()
    output = tmp_path / "out"
    for line in (output / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split()
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    manifests = list(yaml.safe_load_all((output / "install.yaml").read_text()))
    deployment = next(x for x in manifests if x.get("kind") == "Deployment")
    assert deployment["spec"]["template"]["spec"]["containers"][0]["image"] == operator
    with pytest.raises(SystemExit):
        release.main()
