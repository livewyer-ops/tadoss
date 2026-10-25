# ruff: noqa: S603, S607
"""Exercise native composition and Task's exit-code handling without a cluster."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_native_instance_add_preserves_configuration(tmp_path):
    os.environ["AQUA_CONFIG"] = str(ROOT / "aqua.yml")
    source = ROOT / "deploy/environments/local-kind"
    shutil.copytree(source, tmp_path / "env")
    directory = tmp_path / "env"
    path = directory / "kustomization.yaml"
    config = yaml.safe_load(path.read_text())
    config["namePrefix"] = "custom-"
    path.write_text(yaml.safe_dump(config))
    shutil.copytree(directory / "instances/tadoss", directory / "instances/archive")
    for name in ("namespace.yaml", "tadoss.yaml"):
        manifest = directory / "instances/archive" / name
        value = yaml.safe_load(manifest.read_text())
        value["metadata"]["name"] = "archive"
        if name == "tadoss.yaml":
            value["metadata"]["namespace"] = "archive"
        manifest.write_text(yaml.safe_dump(value))
    subprocess.run(
        ["kustomize", "edit", "add", "resource", "instances/archive"],
        cwd=directory,
        check=True,
    )
    rendered = list(
        yaml.safe_load_all(
            subprocess.check_output(["kustomize", "build", str(directory)], text=True)
        )
    )
    assert {x["metadata"]["name"] for x in rendered if x["kind"] == "Tadoss"} == {
        "custom-tadoss",
        "custom-archive",
    }
    assert yaml.safe_load(path.read_text())["resources"] == [
        "instances/tadoss",
        "instances/archive",
    ]


@pytest.mark.parametrize(
    "strict,helm_code,kube_code,success",
    [
        (False, 0, 0, True),
        (False, 2, 1, True),
        (True, 0, 0, True),
        (True, 2, 0, False),
        (True, 0, 1, False),
        (False, 1, 0, False),
        (False, 0, 2, False),
    ],
)
def test_diff_preserves_errors_and_kubeconfig(
    tmp_path, strict, helm_code, kube_code, success
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.jsonl"
    for tool, code in (("helmfile", helm_code), ("kubectl", kube_code)):
        command = bin_dir / tool
        command.write_text(
            "#!/usr/bin/env python3\nimport os, sys, json\n"
            f"with open({str(log)!r}, 'a') as f:\n"
            "    f.write(json.dumps([sys.argv, os.getenv('KUBECONFIG')]) + '\\n')\n"
            f"sys.exit({code})\n"
        )
        command.chmod(0o755)
    selected = str(tmp_path / "selected.kubeconfig")
    result = subprocess.run(
        [
            shutil.which("task"),
            "--taskfile",
            str(ROOT / ".tasks/env.yaml"),
            "diff",
            f"ENV_DIR={ROOT / 'deploy/environments/local-kind'}",
            f"KUBECONFIG={selected}",
            f"STRICT={str(strict).lower()}",
        ],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        timeout=10,
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) == success, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert all(selected in arguments for arguments, _ in calls)
    if success:
        assert len(calls) == 3


def test_kind_uses_selected_environment_and_kubeconfig(tmp_path):
    selected = str(tmp_path / "selected.kubeconfig")
    result = subprocess.run(
        [
            shutil.which("task"),
            "--dry",
            "kind:test",
            "CLUSTER=selected",
            "PROFILE=edge",
            "IMAGE_TAG=selected-build",
            f"KUBECONFIG={selected}",
        ],
        cwd=ROOT,
        env={**os.environ, "KUBECONFIG": "/wrong.kubeconfig"},
        capture_output=True,
        text=True,
        check=True,
    )
    commands = result.stderr
    assert "/wrong.kubeconfig" not in commands
    assert f"--kubeconfig {selected}" in commands
    assert (
        "apply --server-side -k " + str(ROOT / ".local/environments/selected-edge")
        in commands
    )
    assert "livewyer/tadoss-api:selected-build" in commands
    assert "livewyer/tadoss-operator:selected-build" in commands
