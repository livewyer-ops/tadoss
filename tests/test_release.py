import importlib.util
import json
import os
import shutil
import subprocess
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


@pytest.mark.parametrize(
    "tags,code,success",
    [("v0.1.0\n", 0, True), ("v0.1.0\nv0.2.0\n", 0, False), ("", 1, False)],
)
def test_native_preflight_requires_successful_github_absence(
    tmp_path, tags, code, success
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.jsonl"
    for tool, output, status in (("uv", "", 0), ("gh", tags, code)):
        command = bin_dir / tool
        command.write_text(
            "#!/usr/bin/env python3\nimport sys, json\n"
            f"with open({str(log)!r}, 'a') as f:\n"
            "    f.write(json.dumps(sys.argv) + '\\n')\n"
            f"sys.stdout.write({output!r})\nsys.exit({status})\n"
        )
        command.chmod(0o755)
    result = subprocess.run(  # noqa: S603 controlled local test commands
        [
            shutil.which("task"),
            "--taskfile",
            str(ROOT / ".tasks/release.yaml"),
            "preflight",
            "GITHUB_REF_NAME=v0.2.0",
            "GITHUB_REPOSITORY=example/tadoss",
        ],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GITHUB_REF_NAME": "wrong-version",
            "GITHUB_REPOSITORY": "wrong-repository",
        },
        timeout=10,
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) == success, result.stderr
    registry, github = [json.loads(line) for line in log.read_text().splitlines()]
    assert registry[-1] == "v0.2.0"
    assert "repos/example/tadoss/releases" in github
    assert "--paginate" in github


def test_preflight_requires_explicit_not_found(monkeypatch):
    from unittest.mock import MagicMock, Mock
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
    monkeypatch.setattr(preflight, "urlopen", MagicMock())
    with pytest.raises(ValueError, match="already published"):
        preflight.absent(request)


def test_metadata_binds_qualified_images(tmp_path, monkeypatch):
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
            "metadata",
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
    metadata = json.loads((output / "release.json").read_text())
    assert metadata["runtimeImage"] == runtime
    assert metadata["operatorImage"] == operator
    assert json.loads((output / "catalogue.json").read_text())[0]["apiImage"] == runtime
    with pytest.raises(SystemExit):
        release.main()


def test_native_artifacts_pin_images_and_verify_checksums(tmp_path):
    import yaml

    (tmp_path / "src").symlink_to(ROOT / "src", target_is_directory=True)
    (tmp_path / ".git").symlink_to(ROOT / ".git", target_is_directory=True)
    for relative in (
        "scripts/release.py",
        "deploy/operator/install.yaml",
        "deploy/platform/helmfile.yaml.gotmpl",
        "deploy/kind/single-node.yaml",
        "deploy/profiles.yaml",
        "operator/internal/releases/catalogue.json",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    shutil.copyfile(ROOT / ".tasks/release.yaml", tmp_path / "Taskfile.yaml")
    runtime = "registry.example:5000/api@sha256:" + "a" * 64
    operator = "registry.example:5000/operator@sha256:" + "b" * 64
    catalogue = tmp_path / "operator/internal/releases/catalogue.json"
    entries = json.loads(catalogue.read_text())
    entries[0]["apiImage"] = runtime
    catalogue.write_text(json.dumps(entries))
    result = subprocess.run(  # noqa: S603 fixed local Task and fixture paths
        [
            shutil.which("task"),
            "artifacts",
            f"RUNTIME_IMAGE={runtime}",
            f"OPERATOR_IMAGE={operator}",
        ],
        cwd=tmp_path,
        env={**os.environ, "AQUA_CONFIG": str(ROOT / "aqua.yml")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    output = tmp_path / "dist"
    manifests = list(yaml.safe_load_all((output / "install.yaml").read_text()))
    deployment = next(x for x in manifests if x["kind"] == "Deployment")
    assert deployment["spec"]["template"]["spec"]["containers"][0]["image"] == operator
    subprocess.run(["sha256sum", "--check", "SHA256SUMS"], cwd=output, check=True)  # noqa: S607
