"""Refuse replacement of a version and verify promoted image digests."""

import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def absent(request):
    try:
        with urlopen(request, timeout=20):  # noqa: S310 fixed HTTPS endpoints
            raise ValueError("Version is already published; choose a new version")
    except HTTPError as error:
        if error.code != 404:
            raise


def registry_manifest(repository, version):
    with urlopen(
        "https://auth.docker.io/token?service=registry.docker.io&scope=repository:"
        + repository
        + ":pull",
        timeout=20,
    ) as response:
        token = json.load(response)["token"]
    return Request(
        "https://registry-1.docker.io/v2/" + repository + "/manifests/" + version,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": (
                "application/vnd.oci.image.index.v1+json, "
                "application/vnd.docker.distribution.manifest.list.v2+json"
            ),
        },
        method="HEAD",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    release = json.loads(Path("src/app/tadoss/release.json").read_text())
    if args.tag != "v" + release["version"]:
        parser.error("Tag must equal the release version")
    metadata = json.loads(args.verify.read_text()) if args.verify else None
    for group in ("runtime", "operator"):
        repository = release[group + "Repository"]
        request = registry_manifest(repository, release["version"])
        if metadata:
            expected = metadata[group + "Image"]
            if not expected.startswith(repository + "@sha256:"):
                parser.error("Artifact repository differs from release metadata")
            with urlopen(request, timeout=20) as response:  # noqa: S310 fixed HTTPS registry
                if response.headers["Docker-Content-Digest"] != expected.split("@")[1]:
                    parser.error("Promoted tag differs from qualified digest")
        else:
            absent(request)
    if not metadata:
        repository = os.environ["GITHUB_REPOSITORY"]
        absent(
            Request(
                "https://api.github.com/repos/"
                + repository
                + "/releases/tags/"
                + args.tag,
                headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"]},
            )
        )


if __name__ == "__main__":
    main()
