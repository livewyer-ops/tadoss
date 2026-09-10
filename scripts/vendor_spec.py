"""Bundle bbc/tads: uv run --with pyyaml scripts/vendor_spec.py PATH."""

import json
import subprocess
import sys
from pathlib import Path

import yaml

source = Path(sys.argv[1]).resolve()
destination = Path(__file__).resolve().parents[1] / "tadoss" / "contract"


def rewrite(value, base):
    if isinstance(value, list):
        return [rewrite(item, base) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value and not value["$ref"].startswith("#"):
        filename, _, fragment = value["$ref"].partition("#")
        path = base / filename
        if path.parent.name == "schemas":
            return {"$ref": f"#/components/schemas/{path.stem}{fragment}"}
        value = json.loads(path.read_text())
        for key in fragment.lstrip("/").split("/") if fragment else []:
            value = value[key.replace("~1", "/").replace("~0", "~")]
        return value
    return {key: rewrite(item, base) for key, item in value.items()}


spec = rewrite(
    yaml.safe_load((source / "api/TimeAddressableDataStore.yaml").read_text()),
    source / "api",
)
spec["components"]["schemas"] = {
    path.stem: rewrite(json.loads(path.read_text()), path.parent)
    for path in sorted((source / "api/schemas").glob("*.json"))
}
revision = subprocess.check_output(
    ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
).strip()
spec["x-upstream-revision"] = revision
destination.mkdir(parents=True, exist_ok=True)
(destination / "openapi.json").write_text(json.dumps(spec, indent=2) + "\n")
(destination / "LICENCE.md").write_text((source / "LICENCE.md").read_text())
print(f"Bundled BBC TADS {spec['info']['version']} at {revision}")
