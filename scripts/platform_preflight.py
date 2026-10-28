# ruff: noqa: S603, S607
"""Reject unrequested management of shared Helm releases before platform sync."""

import argparse
import json
import subprocess


def foreign_releases(selected, installed):
    return [
        f"{r['namespace']}/{r['name']}"
        for r in selected
        if (r["namespace"], r["name"]) in installed
        and installed[r["namespace"], r["name"]] != "TADOSS-platform"
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--values", required=True)
    args = parser.parse_args()
    selected = json.loads(
        subprocess.check_output(
            [
                "helmfile",
                "--allow-no-matching-release",
                "--kubeconfig",
                args.kubeconfig,
                "--file",
                "deploy/platform/helmfile.yaml.gotmpl",
                "--state-values-file",
                args.values,
                "list",
                "--output",
                "json",
            ],
            text=True,
        )
        or "[]"
    )
    installed = {}
    existing = json.loads(
        subprocess.check_output(
            [
                "helm",
                "--kubeconfig",
                args.kubeconfig,
                "list",
                "--all-namespaces",
                "--max",
                "100000",
                "-o",
                "json",
            ],
            text=True,
        )
    )
    names = {(r["namespace"], r["name"]) for r in existing}
    for r in selected:
        key = r["namespace"], r["name"]
        if key in names:
            status = json.loads(
                subprocess.check_output(
                    [
                        "helm",
                        "--kubeconfig",
                        args.kubeconfig,
                        "status",
                        r["name"],
                        "-n",
                        r["namespace"],
                        "-o",
                        "json",
                    ],
                    text=True,
                )
            )
            installed[key] = status["info"]["description"]
    foreign = foreign_releases(selected, installed)
    if foreign:
        parser.exit(
            1,
            "Shared or unmarked releases selected: "
            + ", ".join(foreign)
            + ". Set their platform.*.enabled=false to reuse them without changes.\n",
        )


if __name__ == "__main__":
    main()
