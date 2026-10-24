import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "environment", ROOT / "scripts/environment.py"
)
environment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(environment)


def test_composition_and_multiple_instances(tmp_path):
    directory = tmp_path / "site"
    environment.init_environment(directory, "edge", "example.test", "0.1.0")
    with pytest.raises(ValueError, match="already exists"):
        environment.init_environment(directory, "edge", "example.test", "0.1.0")
    environment.init_instance(directory, "second", "tenant-two", "0.1.0")
    selections = environment.instances(directory, "second")
    assert [(namespace, name) for _, namespace, name in selections] == [
        ("tenant-two", "second")
    ]
    resource = yaml.safe_load((directory / "instances/second/tadoss.yaml").read_text())
    assert resource["spec"] == {"version": "0.1.0"}


@pytest.mark.parametrize(
    "strict,expected", [(False, [(0, 2), (0, 1), (0, 1)]), (True, [(0,), (0,), (0,)])]
)
def test_diff_exit_codes_are_explicit(tmp_path, strict, expected):
    calls = []
    with patch.object(
        environment, "run", side_effect=lambda args, **kw: calls.append((args, kw))
    ):
        environment.operate(
            "diff", tmp_path, Path("/selected.kubeconfig"), strict=strict
        )
    assert [kw["allowed_codes"] for _, kw in calls] == expected
    assert all("/selected.kubeconfig" in args for args, _ in calls)


def test_bootstrap_uses_sync_and_consistent_server_side_apply(tmp_path):
    calls = []
    with patch.object(
        environment, "run", side_effect=lambda args, **_kw: calls.append(args)
    ):
        environment.operate("apply", tmp_path, Path("relative.kubeconfig"))
    assert calls[0][-1] == "sync"
    assert all(str(Path("relative.kubeconfig").resolve()) in args for args in calls)
    assert all("--server-side" in args for args in calls if "apply" in args)


@pytest.mark.parametrize(
    "image,replacement",
    [
        (
            "registry.example:5000/team/operator:v1.2",
            {"newName": "registry.example:5000/team/operator", "newTag": "v1.2"},
        ),
        (
            "registry.example:5000/operator@sha256:" + "a" * 64,
            {
                "newName": "registry.example:5000/operator",
                "digest": "sha256:" + "a" * 64,
            },
        ),
    ],
)
def test_operator_image_overrides(tmp_path, image, replacement):
    directory = tmp_path / "site"
    environment.init_environment(
        directory, "edge", "example.test", "0.1.0", operator_image=image
    )
    overlay = yaml.safe_load((directory / "operator/kustomization.yaml").read_text())
    assert overlay["images"] == [{"name": "livewyer/tadoss-operator", **replacement}]
    assert (
        overlay["configMapGenerator"][0]["options"]["labels"]["app.kubernetes.io/name"]
        == "tadoss-instance-defaults"
    )


@pytest.mark.parametrize(
    "image",
    [
        "operator",
        "registry.example:5000/team/operator",
        "operator:",
        "operator@sha256:1234",
        "operator@sha512:" + "a" * 64,
        "https://example/operator:v1",
        "operator:bad tag",
        "registry:99999/operator:v1",
    ],
)
def test_invalid_operator_image_leaves_no_files(tmp_path, image):
    directory = tmp_path / "site"
    with pytest.raises(ValueError):
        environment.init_environment(
            directory, "edge", "example.test", "0.1.0", operator_image=image
        )
    assert not directory.exists()
