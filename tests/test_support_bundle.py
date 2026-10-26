import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "support_bundle", Path(__file__).resolve().parents[1] / "scripts/support_bundle.py"
)
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


def test_allowlist_discards_secrets_payloads_urls_and_annotations():
    resource = {
        "kind": "Pod",
        "metadata": {
            "name": "api",
            "namespace": "tads",
            "annotations": {
                "kubectl.kubernetes.io/last-applied-configuration": "password"
            },
        },
        "spec": {"containers": [{"env": [{"value": "secret"}]}]},
        "status": {
            "phase": "Running",
            "currentVersion": "0.1.0",
            "pendingVersion": "0.2.0",
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True",
                    "message": "https://user:password@host/?access_token=secret",
                }
            ],
        },
        "data": {"token": "secret"},
    }
    result = bundle.sanitise(resource)
    assert result["status"]["phase"] == "Running"
    assert result["status"]["currentVersion"] == "0.1.0"
    assert result["status"]["pendingVersion"] == "0.2.0"
    text = json.dumps(result)
    for excluded in ("password", "secret", "access_token", "last-applied", "https://"):
        assert excluded not in text
