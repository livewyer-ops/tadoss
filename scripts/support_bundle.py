"""Collect an allowlisted diagnostic snapshot without reading Secret values."""

import argparse
import json
import subprocess
from pathlib import Path

KINDS = (
    "tadosses",
    "deployments",
    "jobs",
    "pods",
    "clusters.postgresql.cnpg.io",
    "scheduledbackups.postgresql.cnpg.io",
)
STATUS_FIELDS = (
    "phase",
    "observedGeneration",
    "readyReplicas",
    "updatedReplicas",
    "availableReplicas",
    "workerReadyReplicas",
    "currentVersion",
    "schemaJob",
    "appliedDefaultsRevision",
)


def sanitise(resource):
    metadata = resource.get("metadata", {})
    status = resource.get("status", {})
    return {
        "apiVersion": resource.get("apiVersion"),
        "kind": resource.get("kind"),
        "metadata": {
            key: metadata[key]
            for key in ("name", "namespace", "generation", "creationTimestamp")
            if key in metadata
        },
        "status": {
            **{key: status[key] for key in STATUS_FIELDS if key in status},
            "conditions": [
                {
                    key: condition[key]
                    for key in (
                        "type",
                        "status",
                        "reason",
                        "observedGeneration",
                        "lastTransitionTime",
                    )
                    if key in condition
                }
                for condition in status.get("conditions", [])
            ],
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--output", type=Path, default=Path("reports/support.json"))
    args = parser.parse_args()
    resources = []
    unavailable = []
    for kind in KINDS:
        result = subprocess.run(  # noqa: S603 fixed executable, argv only
            [  # noqa: S607 developer tool from PATH
                "kubectl",
                "--kubeconfig",
                args.kubeconfig,
                "get",
                kind,
                "-n",
                args.namespace,
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            unavailable.append(kind)
            continue
        resources.extend(
            sanitise(item) for item in json.loads(result.stdout).get("items", [])
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"resources": resources, "unavailableKinds": unavailable}, indent=2)
        + "\n"
    )
    if not resources:
        parser.exit(
            1, "No resources could be collected; check namespace and permissions\n"
        )


if __name__ == "__main__":
    main()
