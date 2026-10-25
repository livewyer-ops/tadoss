import pytest

from tests.support.chainsaw import validate_command, validate_document


@pytest.mark.parametrize("phase", ["try", "catch", "finally"])
@pytest.mark.parametrize(
    "operation",
    [
        {"script": {"content": "echo inline"}},
        {"command": {"entrypoint": "/bin/sh", "args": ["-c", "echo inline"]}},
        {"command": {"entrypoint": "python3", "args": ["-u", "-c", "print('inline')"]}},
        {"apply": {"file": "missing.yaml"}},
    ],
)
def test_invalid_operations_are_rejected_in_every_phase(tmp_path, phase, operation):
    document = {"kind": "Test", "spec": {"steps": [{phase: [operation]}]}}
    with pytest.raises(AssertionError):
        validate_document(document, tmp_path / "chainsaw-test.yaml")


def test_suite_cleanup_and_resource_entrypoints_are_checked(tmp_path):
    for document in [
        {"kind": "Test", "spec": {"catch": [{"script": {"content": "echo inline"}}]}},
        {"kind": "Pod", "spec": {"containers": [{"command": ["/bin/bash"]}]}},
        {
            "kind": "Pod",
            "spec": {
                "containers": [
                    {"command": ["python"], "args": ["-c", "print('inline')"]}
                ]
            },
        },
        {"kind": "ConfigMap", "data": {"check.py": "print('embedded')"}},
    ]:
        with pytest.raises(AssertionError):
            validate_document(document, tmp_path / "fixture.yaml")


@pytest.mark.parametrize(
    "command",
    [
        ["python", "/tests/check.py", "-c", "fixture.toml"],
        ["python3", "-u", "-m", "pytest", "-c", "pytest.ini"],
        ["kubectl", "exec", "deployment/db", "--", "psql"],
        ["kubectl", "logs", "deployment/api"],
        ["kubectl", "set", "env", "deployment/operator", "WATCH_NAMESPACES=tads"],
    ],
)
def test_source_files_modules_and_platform_commands_are_allowed(command):
    validate_command(command)


@pytest.mark.parametrize(
    "command",
    [
        ["kubectl", "apply", "-f", "fixture.yaml"],
        [
            "kubectl",
            "exec",
            "deployment/api",
            "--",
            "python",
            "-u",
            "-c",
            "print('inline')",
        ],
        ["kubectl", "exec", "deployment/api", "--", "bash"],
    ],
)
def test_kubernetes_orchestration_and_remote_scripts_are_rejected(command):
    with pytest.raises(AssertionError):
        validate_command(command)


def test_existing_fixture_references_are_allowed(tmp_path):
    (tmp_path / "fixture.yaml").touch()
    validate_document(
        {
            "kind": "Test",
            "spec": {"steps": [{"try": [{"apply": {"file": "fixture.yaml"}}]}]},
        },
        tmp_path / "chainsaw-test.yaml",
    )
