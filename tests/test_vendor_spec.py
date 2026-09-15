import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_vendor_spec():
    spec = importlib.util.spec_from_file_location(
        "vendor_spec", ROOT / "scripts" / "vendor_spec.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VendorSpecTests(unittest.TestCase):
    def test_nested_file_references_resolve(self):
        try:
            vendor_spec = load_vendor_spec()
        except ImportError as exc:
            self.skipTest(f"vendor_spec dependencies missing: {exc}")
        with tempfile.TemporaryDirectory() as destination:
            spec = vendor_spec.bundle(
                ROOT / "tests" / "fixtures" / "tads", Path(destination), "abc123"
            )
            written = json.loads(Path(destination, "openapi.json").read_text())
        self.assertEqual(spec, written)
        self.assertEqual(spec["x-upstream-revision"], "abc123")
        parameter = spec["paths"]["/things"]["get"]["parameters"][0]
        self.assertEqual(parameter["schema"], {"$ref": "#/components/schemas/other"})
        response = spec["paths"]["/things"]["get"]["responses"]["200"]
        self.assertEqual(
            response["content"]["application/json"]["schema"],
            {"$ref": "#/components/schemas/thing"},
        )
        self.assertEqual(
            spec["components"]["schemas"]["thing"]["properties"]["other"],
            {"$ref": "#/components/schemas/other"},
        )
