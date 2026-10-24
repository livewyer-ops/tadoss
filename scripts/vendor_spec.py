"""Bundle bbc/tads into one JSON document.

Usage: uv run --with pyyaml scripts/vendor_spec.py PATH, where PATH is a checkout of
https://github.com/bbc/tads at the revision to pin.
"""

import argparse
import json
import shutil
import subprocess
from pathlib import Path

import yaml


def rewrite(value, base):
    """Resolve file references: schemas become component references, other
    files are inlined and rewritten in turn so nested references resolve."""
    if isinstance(value, list):
        return [rewrite(item, base) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value and not value["$ref"].startswith("#"):
        filename, _, fragment = value["$ref"].partition("#")
        path = base / filename
        if path.parent.name == "schemas":
            return {"$ref": f"#/components/schemas/{path.stem}{fragment}"}
        target = json.loads(path.read_text())
        for key in fragment.lstrip("/").split("/") if fragment else []:
            target = target[key.replace("~1", "/").replace("~0", "~")]
        return rewrite(target, path.parent)
    return {key: rewrite(item, base) for key, item in value.items()}


def bundle(source, destination, revision):
    spec = rewrite(
        yaml.safe_load((source / "api/TimeAddressableDataStore.yaml").read_text()),
        source / "api",
    )
    spec["components"]["schemas"] = {
        path.stem: rewrite(json.loads(path.read_text()), path.parent)
        for path in sorted((source / "api/schemas").glob("*.json"))
    }
    spec["x-upstream-revision"] = revision
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "openapi.json").write_text(json.dumps(spec, indent=2) + "\n")
    (destination / "LICENCE.md").write_text((source / "LICENCE.md").read_text())
    return spec


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="checkout of bbc/tads")
    args = parser.parse_args()
    source = args.source.resolve()
    git = shutil.which("git") or "git"
    revision = subprocess.check_output(  # noqa: S603  fixed argv, no shell
        [git, "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    destination = (
        Path(__file__).resolve().parents[1] / "src" / "app" / "tadoss" / "contract"
    )
    spec = bundle(source, destination, revision)
    print(f"Bundled BBC TADS {spec['info']['version']} at {revision}")


if __name__ == "__main__":
    main()
