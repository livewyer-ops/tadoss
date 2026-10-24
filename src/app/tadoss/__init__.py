import json
from pathlib import Path

RELEASE = json.loads((Path(__file__).parent / "release.json").read_text())
__version__ = RELEASE["version"]
