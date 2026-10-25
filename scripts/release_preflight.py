"""Refuse publication unless registry manifests explicitly return HTTP 404."""

import argparse
import json
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
    args = parser.parse_args()
    release = json.loads(Path("src/app/tadoss/release.json").read_text())
    if args.tag != "v" + release["version"]:
        parser.error("Tag must equal the release version")
    for group in ("runtime", "operator"):
        absent(registry_manifest(release[group + "Repository"], release["version"]))


if __name__ == "__main__":
    main()
