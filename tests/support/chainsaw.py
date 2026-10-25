"""Keep Chainsaw orchestration native and fixture programmes in source files."""

import re
from pathlib import Path


def mappings(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from mappings(child)


def validate_command(command):
    if not command:
        return
    executable = Path(command[0]).name
    assert executable not in {"sh", "bash", "dash", "ash", "zsh"}, (
        "Shell entrypoints are not allowed"
    )
    if re.fullmatch(r"python[\d.]*", executable):
        for argument in command[1:]:
            assert not argument.startswith("-c"), (
                "Invoke a Python source file or module, not inline code"
            )
            if argument == "-m" or not argument.startswith("-"):
                break
    if executable == "kubectl":
        args = command[1:]
        if args[:2] == ["set", "env"] or args[:1] == ["logs"]:
            return
        assert args[:1] == ["exec"] and "--" in args, (
            "Use native Chainsaw operations for Kubernetes orchestration"
        )
        remote = args[args.index("--") + 1 :]
        assert remote, "kubectl exec requires a command"
        validate_command(remote)


def validate_document(document, path):
    if not document:
        return
    for node in mappings(document):
        if document.get("kind") == "Test":
            assert "script" not in node, "Use native Chainsaw operations"
            if (reference := node.get("file")) and not any(
                marker in reference for marker in ("$", "(", "*", "?", "[")
            ):
                assert (path.parent / reference).is_file(), (
                    f"Referenced file does not exist: {reference}"
                )
        command = node.get("command")
        if isinstance(command, dict):
            validate_command([command["entrypoint"], *command.get("args", [])])
        elif isinstance(command, list):
            validate_command([*command, *node.get("args", [])])
        if node.get("kind") == "ConfigMap":
            for name, content in node.get("data", {}).items():
                if name.endswith((".py", ".sh")):
                    assert re.fullmatch(r"\(\$[A-Za-z]+\)", content), (
                        "Keep fixture programmes in source files"
                    )
