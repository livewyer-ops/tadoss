"""Identify the inputs used by local runtime and operator builds."""

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
paths = [
    ROOT / name
    for name in (
        "Dockerfile",
        "LICENSE",
        "NOTICE",
        "README.md",
        "src/pyproject.toml",
        "src/uv.lock",
        "operator/Dockerfile",
        "operator/go.mod",
        "operator/go.sum",
    )
]
paths.extend(
    p
    for p in (ROOT / "src/app").rglob("*")
    if p.is_file() and p.suffix in {".py", ".json", ".md"}
)
paths.extend(
    p
    for p in (ROOT / "operator").rglob("*")
    if p.is_file() and p.suffix in {".go", ".json"} and "bin" not in p.parts
)
digest = hashlib.sha256()
for path in sorted(paths):
    digest.update(str(path.relative_to(ROOT)).encode())
    digest.update(path.read_bytes())
print("dev-" + digest.hexdigest()[:12])
