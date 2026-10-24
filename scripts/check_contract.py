"""Check the pinned contract and emit the current conformance inventory."""

import json
import subprocess
import tempfile
from pathlib import Path

from tadoss.api import published_contract
from tadoss.api.routes import HANDLERS
from tadoss.domain.validation import SPEC
from vendor_spec import bundle

ROOT = Path(__file__).resolve().parents[1]
source = ROOT / "src/vendor/bbc-tads"
revision = subprocess.check_output(  # noqa: S603 fixed executable, argv only
    [  # noqa: S607 developer tool from PATH
        "git",
        "-C",
        str(source),
        "rev-parse",
        "HEAD",
    ],
    text=True,
).strip()
if revision != SPEC["x-upstream-revision"]:
    raise SystemExit("Vendor revision differs from the bundled contract")
with tempfile.TemporaryDirectory() as directory:
    regenerated = bundle(source, Path(directory), revision)
    if regenerated != SPEC:
        raise SystemExit("Regenerate the bundled contract")
if published_contract()["paths"] != SPEC["paths"] or set(HANDLERS) != set(
    SPEC["paths"]
):
    raise SystemExit(
        "Published paths and handlers must match the complete pinned contract"
    )
inventory = {
    "upstreamRevision": revision,
    "apiVersion": SPEC["info"]["version"],
    "operations": [
        {
            "method": method.upper(),
            "path": path,
            "operationId": definition.get("operationId"),
            "handlerRegistered": path in HANDLERS,
        }
        for path, operations in SPEC["paths"].items()
        for method, definition in operations.items()
        if method != "parameters"
    ],
}
(ROOT / "reports").mkdir(exist_ok=True)
(ROOT / "reports/tads-conformance.json").write_text(
    json.dumps(inventory, indent=2) + "\n"
)
print(f"Contract verified: {len(inventory['operations'])} operations at {revision}")
