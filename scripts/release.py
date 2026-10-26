"""Validate release inputs and write metadata for native artifact assembly."""

import argparse
import json
import re
import subprocess
from pathlib import Path

import yaml
from tadoss import __version__
from tadoss.db.migrations import MIGRATIONS
from tadoss.domain.validation import SPEC

ROOT = Path(__file__).resolve().parents[1]
RELEASE = json.loads((ROOT / "src/app/tadoss/release.json").read_text())


def check():
    if (
        __version__ != RELEASE["version"]
        or MIGRATIONS[-1][0] != RELEASE["schemaTarget"]
        or SPEC["x-upstream-revision"] != RELEASE["upstreamRevision"]
        or SPEC["info"]["version"] != RELEASE["apiVersion"]
    ):
        raise ValueError("Runtime, specification and release metadata disagree")
    catalogue = json.loads(
        (ROOT / "operator/internal/releases/catalogue.json").read_text()
    )
    versions = {entry["version"] for entry in catalogue}
    if len(versions) != len(catalogue):
        raise ValueError("Operator catalogue contains duplicate versions")
    for entry in catalogue:
        predecessors = entry.get("upgradeFrom")
        if (
            not isinstance(predecessors, list)
            or any(
                not isinstance(value, str)
                or value not in versions
                or value == entry["version"]
                for value in predecessors
            )
            or len(set(predecessors)) != len(predecessors)
        ):
            raise ValueError("Operator catalogue has invalid upgradeFrom")
    current = next(
        (entry for entry in catalogue if entry["version"] == RELEASE["version"]), None
    )
    if current is None or current["schemaTarget"] != RELEASE["schemaTarget"]:
        raise ValueError("Operator catalogue differs from current release")
    platform = "\n".join(
        (ROOT / path).read_text()
        for path in (
            "deploy/platform/helmfile.yaml.gotmpl",
            "deploy/authentik/helmfile.yaml.gotmpl",
        )
    )
    for chart, version in RELEASE["platformCharts"].items():
        pattern = (
            r"chart: \S*/"
            + re.escape(chart)
            + r"\s+version: "
            + re.escape(version)
            + r"(?:\s|$)"
        )
        if not re.search(pattern, platform):
            raise ValueError("Platform chart differs from release metadata: " + chart)

    return catalogue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "catalogue", "metadata"])
    parser.add_argument("--runtime-image")
    parser.add_argument("--operator-image")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    catalogue = check()
    if args.command == "check":
        print("Release metadata is consistent")
        return
    for image in (
        [args.runtime_image]
        if args.command == "catalogue"
        else [args.runtime_image, args.operator_image]
    ):
        if not image or not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image):
            parser.error("Release images must use immutable sha256 digests")
    for entry in catalogue:
        if entry["version"] == RELEASE["version"]:
            entry["apiImage"] = args.runtime_image
    if args.command == "catalogue":
        (ROOT / "operator/internal/releases/catalogue.json").write_text(
            json.dumps(catalogue, indent=2) + "\n"
        )
        return
    embedded = json.loads(
        (ROOT / "operator/internal/releases/catalogue.json").read_text()
    )
    if embedded != catalogue:
        parser.error(
            "Build the operator with this exact runtime catalogue "
            "before assembling artifacts"
        )
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("Artifact directory is not empty; use a new output directory")
    args.output.mkdir(parents=True, exist_ok=True)
    metadata = {
        **RELEASE,
        "runtimeImage": args.runtime_image,
        "operatorImage": args.operator_image,
        "sourceRevision": subprocess.check_output(
            [  # noqa: S607 developer tool from PATH
                "git",
                "rev-parse",
                "HEAD",
            ],
            cwd=ROOT,
            text=True,
        ).strip(),
    }
    (args.output / "release.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (args.output / "catalogue.json").write_text(json.dumps(catalogue, indent=2) + "\n")
    (args.output / "compatibility.yaml").write_text(
        yaml.safe_dump(
            {
                "version": RELEASE["version"],
                "schemaTarget": RELEASE["schemaTarget"],
                "upgradeFrom": next(
                    entry["upgradeFrom"]
                    for entry in catalogue
                    if entry["version"] == RELEASE["version"]
                ),
                "platformCharts": RELEASE["platformCharts"],
                "qualifiedKubernetesImage": yaml.safe_load(
                    (ROOT / "deploy/kind/single-node.yaml").read_text()
                )["nodes"][0]["image"],
                "architectures": ["linux/amd64", "linux/arm64"],
                "profiles": list(
                    yaml.safe_load((ROOT / "deploy/profiles.yaml").read_text())
                ),
            },
            sort_keys=False,
        )
    )


if __name__ == "__main__":
    main()
