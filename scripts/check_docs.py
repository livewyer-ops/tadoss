# ruff: noqa: S101
"""Check repository documentation links, task examples and profile examples."""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
paths = [
    *ROOT.glob("*.md"),
    ROOT / "src/README.md",
    ROOT / "operator/README.md",
    ROOT / "deploy/README.md",
    *ROOT.glob("docs/**/*.md"),
]
tasks = {"setup", "check"}
for path in (ROOT / ".tasks").glob("*.yaml"):
    tasks.update(
        path.stem + ":" + name for name in yaml.safe_load(path.read_text())["tasks"]
    )
for path in paths:
    content = path.read_text()
    for target in re.findall(r"\]\(([^)]+)\)", content):
        if "://" in target or target.startswith("#") or target.startswith("mailto:"):
            continue
        if not (path.parent / target.split("#")[0]).exists():
            raise SystemExit(f"{path.relative_to(ROOT)}: missing link {target}")
    for task in re.findall(r"\btask ([a-z][a-z:-]+)", content):
        if task not in tasks:
            raise SystemExit(f"{path.relative_to(ROOT)}: unknown task {task}")
versions = {
    item["version"]
    for item in json.loads(
        (ROOT / "operator/internal/releases/catalogue.json").read_text()
    )
}
for profile in yaml.safe_load((ROOT / "deploy/profiles.yaml").read_text()):
    env = ROOT / "deploy/environments" / profile
    defaults = yaml.safe_load((env / "operator/defaults.yaml").read_text())
    assert defaults["profile"] == profile, profile
    for path in (env / "instances").glob("*/tadoss.yaml"):
        instance = yaml.safe_load(path.read_text())
        assert instance["spec"]["version"] in versions, path
for path in (ROOT / "operator/test/chainsaw").rglob("*.yaml"):
    for document in yaml.safe_load_all(path.read_text()):
        if document and document.get("kind") == "ConfigMap":
            for name, content in document.get("data", {}).items():
                if name.endswith(".py"):
                    compile(content, str(path) + ":" + name, "exec")
print("Documentation links, task names, profiles and embedded Python are consistent")
