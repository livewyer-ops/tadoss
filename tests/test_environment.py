# ruff: noqa: S603, S607
"""Exercise native composition and Task's exit-code handling without a cluster."""

import base64
import json
import os
import shutil
import subprocess
import sys
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
            "PROFILE=single-server",
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
    environment = ROOT / ".local/environments/selected-single-server"
    assert f"apply --server-side -k {environment}" in commands
    assert "livewyer/tadoss-api:selected-build" in commands
    assert "livewyer/tadoss-operator:selected-build" in commands


@pytest.fixture
def credentials_task(tmp_path):
    shutil.copy(ROOT / ".tasks/env.yaml", tmp_path / "Taskfile.yaml")
    (tmp_path / "scripts").symlink_to(ROOT / "scripts", target_is_directory=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.jsonl"
    command = bin_dir / "kubectl"
    command.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        f"with open({str(calls)!r}, 'a') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if 'kustomize' in sys.argv:\n"
        "    items = json.loads(os.environ['TEST_INSTANCE'])['items']\n"
        "    print('\\n---\\n'.join(json.dumps(r) for r in items))\n"
        "elif '-f' in sys.argv:\n"
        "    sys.stdin.read()\n"
        "    print(os.environ['TEST_INSTANCE'])\n"
        "else:\n"
        "    sys.stdout.write(os.environ['TEST_TOKEN'])\n"
        "    sys.exit(int(os.environ['TEST_SECRET_CODE']))\n"
    )
    command.chmod(0o755)
    selected = tmp_path / "selected 'quoted' cluster.kubeconfig"
    instance = {
        "kind": "Tadoss",
        "metadata": {"namespace": "archive-space", "name": "prefix-archive"},
        "status": {
            "endpoint": "https://archive.example.test",
            "tokenSecret": "custom-token",
        },
    }

    def run(
        resources=None,
        token=b"test-token",
        code=0,
        selected_instance="prefix-archive",
        fields=None,
        auth_method="auto",
    ):
        result = subprocess.run(
            [
                shutil.which("task"),
                "credentials",
                f"ENV_DIR={tmp_path / 'environment'}",
                f"INSTANCE={selected_instance}",
                f"KUBECONFIG={selected}",
                f"AUTH_METHOD={auth_method}",
            ],
            cwd=tmp_path,
            env={
                **os.environ,
                "AQUA_CONFIG": str(ROOT / "aqua.yml"),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "TEST_INSTANCE": json.dumps(
                    {"kind": "List", "items": resources or [instance]}
                ),
                "TEST_TOKEN": json.dumps(
                    {
                        key: base64.b64encode(value).decode()
                        for key, value in (fields or {"token": token}).items()
                    }
                ),
                "TEST_SECRET_CODE": str(code),
                # Exercise shell output without Task's GitHub stdout annotations.
                "GITHUB_ACTIONS": "false",
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result

    return run, instance, selected, calls


def test_credentials_exports_round_trip_without_shell_execution(credentials_task):
    run, instance, selected, calls = credentials_task
    token = b"quotes'\" $UNSET `exit 1` $(exit 2) ;\ntrailing newline\n"
    result = run(token=token)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.startswith(
        "# Authentication: URL access token (query parameter: access_token)\n"
    )
    restored = subprocess.check_output(
        [
            "sh",
            "-eu",
            "-c",
            result.stdout + '\nexec "$1" -c "$2"',
            "exports",
            sys.executable,
            "import json, os; print(json.dumps({k: os.environ[k] for k in "
            "['KUBECONFIG', 'TADOSS_URL', 'TADOSS_API_TOKEN']}))",
        ],
        text=True,
    )
    assert json.loads(restored) == {
        "KUBECONFIG": str(selected),
        "TADOSS_URL": instance["status"]["endpoint"],
        "TADOSS_API_TOKEN": token.decode(),
    }
    arguments = [json.loads(line) for line in calls.read_text().splitlines()]
    assert all(str(selected) in args for args in arguments)
    assert arguments[-1][-7:] == [
        "-n",
        "archive-space",
        "get",
        "secret",
        "custom-token",
        "-o",
        "jsonpath={.data}",
    ]


@pytest.mark.parametrize(
    "failure", ["unready", "multiple", "denied", "empty", "nul", "utf8", "unselected"]
)
def test_credentials_fail_without_partial_exports(credentials_task, failure):
    run, instance, _, _ = credentials_task
    kwargs = {}
    if failure == "unready":
        instance["status"].pop("endpoint")
    elif failure == "multiple":
        kwargs["resources"] = [instance, instance]
    elif failure == "denied":
        kwargs["code"] = 1
    elif failure == "empty":
        kwargs["token"] = b""
    elif failure == "nul":
        kwargs["token"] = b"bad\0token"
    elif failure == "utf8":
        kwargs["token"] = b"\xff"
    else:
        kwargs["selected_instance"] = ""
    result = run(**kwargs)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "test-token" not in result.stderr


def test_oauth_credentials_are_explicit_and_clear_query_tokens(credentials_task):
    run, instance, _, calls = credentials_task
    instance["spec"] = {
        "auth": {
            "tokenEnabled": False,
            "oauth2": {
                "issuer": "https://auth.example/application/o/tadoss/",
                "clientCredentialsSecret": "tadoss-oauth-client",
            },
        }
    }
    result = run(
        fields={
            "client_id": b"checker",
            "client_secret": b"secret'$(exit 1)",
            "token_endpoint": b"https://auth.example/application/o/token/",
            "scopes": b"tads-api/read tads-api/write tads-api/delete",
        }
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    restored = subprocess.check_output(
        [
            "sh",
            "-eu",
            "-c",
            result.stdout + '\nexec "$1" -c "$2"',
            "exports",
            sys.executable,
            "import os,json; print(json.dumps({k:v for k,v in os.environ.items() "
            "if k.startswith('TADOSS_')}))",
        ],
        env=os.environ | {"TADOSS_API_TOKEN": "stale-query-token"},
        text=True,
    )
    values = json.loads(restored)
    assert "TADOSS_API_TOKEN" not in values
    assert values["TADOSS_AUTH_METHOD"] == "oauth"
    assert values["TADOSS_OAUTH_CLIENT_SECRET"] == "secret'$(exit 1)"
    assert values["TADOSS_OAUTH_CLIENT_AUTH_METHOD"] == "post"
    assert "tadoss-oauth-client" in calls.read_text()
    denied = run(auth_method="query")
    assert denied.returncode != 0 and denied.stdout == ""
    assert "disabled" in denied.stderr


def test_oauth_credentials_do_not_fall_back_when_unavailable(credentials_task):
    run, instance, _, _ = credentials_task
    instance["spec"] = {"auth": {"oauth2": {"issuer": "https://auth.example"}}}
    result = run()
    assert result.returncode != 0 and result.stdout == ""
    assert "clientCredentialsSecret" in result.stderr


@pytest.mark.parametrize("lookup", ["absent", "present", "denied"])
def test_authentik_bootstrap_preserves_secrets_and_fails_closed(tmp_path, lookup):
    binary = tmp_path / "kubectl"
    log = tmp_path / "calls.jsonl"
    binary.write_text(
        "#!/usr/bin/env python3\nimport json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "if 'namespace' in args: sys.exit(0)\n"
        "if 'get' in args:\n"
        "    if os.environ['LOOKUP'] == 'denied': sys.exit(1)\n"
        "    if os.environ['LOOKUP'] == 'present': print('secret/existing')\n"
        "    sys.exit(0)\n"
        "files = [pathlib.Path(a.split('=', 2)[2]) for a in args "
        "if a.startswith('--from-file=')]\n"
        "assert all(p.stat().st_mode & 0o077 == 0 for p in files)\n"
        "assert all(len(p.read_bytes()) >= 64 for p in files)\n"
        f"with open({str(log)!r}, 'a') as f: "
        "f.write(json.dumps(args) + '\\n')\n"
    )
    binary.chmod(0o755)
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/authentik_bootstrap.sh"),
            str(tmp_path / "cluster.yaml"),
            "tads",
            "example",
            "auth.example.com",
        ],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "LOOKUP": lookup,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) == (lookup != "denied")
    calls = (
        [json.loads(line) for line in log.read_text().splitlines()]
        if log.exists()
        else []
    )
    assert len(calls) == (2 if lookup == "absent" else 0)
    assert result.stdout == ""
    for call in calls:
        assert not any(a.startswith("--from-literal=client_secret=") for a in call)
        for arg in call:
            if arg.startswith("--from-file="):
                assert not Path(arg.split("=", 2)[2]).exists()
