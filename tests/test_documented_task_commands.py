# ruff: noqa: S607
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTED_PATHS = [
    *ROOT.glob("*.md"),
    ROOT / "src/README.md",
    ROOT / "operator/README.md",
    ROOT / "deploy/README.md",
    *ROOT.glob("docs/**/*.md"),
]
TASK_REFERENCE = re.compile(
    r"(?:`task\s+([A-Za-z0-9:_-]+|-l)\b|^\s*task\s+([A-Za-z0-9:_-]+|-l)\b)",
    re.MULTILINE,
)


def available_tasks(directory: Path) -> set[str]:
    result = subprocess.run(
        ["task", "--list-all", "--json"],
        cwd=directory,
        capture_output=True,
        check=True,
        text=True,
    )
    tasks = json.loads(result.stdout)["tasks"]
    return {name for task in tasks for name in [task["name"], *task.get("aliases", [])]}


def test_documented_task_commands_are_supported_entry_points() -> None:
    available = available_tasks(ROOT)
    missing = sorted(
        {
            command
            for path in DOCUMENTED_PATHS
            for command in documented_task_commands(path)
            if command not in available
        }
    )
    assert missing == []


def documented_task_commands(path: Path) -> set[str]:
    commands = set()
    for match in TASK_REFERENCE.finditer(path.read_text(encoding="utf-8")):
        command = match.group(1) or match.group(2)
        if command == "-l":
            continue
        commands.add(command)
    return commands


def test_task_listing_includes_aliases_and_excludes_internal_tasks(tmp_path):
    (tmp_path / "Taskfile.yaml").write_text(
        "version: '3'\nincludes:\n  demo: child.yaml\ntasks:\n"
        "  public:\n    aliases: [short]\n    cmds: [echo public]\n"
        "  hidden:\n    internal: true\n    cmds: [echo hidden]\n"
    )
    (tmp_path / "child.yaml").write_text(
        "version: '3'\ntasks:\n  run:\n    cmds: [echo child]\n"
    )
    assert available_tasks(tmp_path) == {"public", "short", "demo:run"}


def test_task_references_include_inline_and_fenced_commands(tmp_path):
    path = tmp_path / "guide.md"
    path.write_text(
        "Run `task operator:test` or `task -l`.\n\n```sh\n"
        "task kind:test PROFILE=local-kind\n```\n"
    )
    assert documented_task_commands(path) == {"operator:test", "kind:test"}
