"""Check and assemble one exact TADOSS release."""

import argparse
import hashlib
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
    if (
        len(catalogue) != 1
        or catalogue[0]["version"] != RELEASE["version"]
        or catalogue[0]["schemaTarget"] != RELEASE["schemaTarget"]
    ):
        raise ValueError("Operator catalogue differs from current release")
    platform = (ROOT / "deploy/platform/helmfile.yaml.gotmpl").read_text()
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "catalogue", "artifacts"])
    parser.add_argument("--runtime-image")
    parser.add_argument("--operator-image")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    check()
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
    catalogue = [
        {
            "version": RELEASE["version"],
            "apiImage": args.runtime_image,
            "schemaTarget": RELEASE["schemaTarget"],
        }
    ]
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
    manifests = list(
        yaml.safe_load_all((ROOT / "deploy/operator/install.yaml").read_text())
    )
    for manifest in manifests:
        if (
            manifest
            and manifest["kind"] == "Deployment"
            and manifest["metadata"]["name"] == "tadoss-operator"
        ):
            manifest["spec"]["template"]["spec"]["containers"][0]["image"] = (
                args.operator_image
            )
    (args.output / "install.yaml").write_text(
        yaml.safe_dump_all(manifests, sort_keys=False)
    )
    paths = ["release.json", "catalogue.json", "compatibility.yaml", "install.yaml"]
    (args.output / "SHA256SUMS").write_text(
        "".join(
            hashlib.sha256((args.output / name).read_bytes()).hexdigest()
            + "  "
            + name
            + "\n"
            for name in paths
        )
    )


if __name__ == "__main__":
    main()
