"""Print shell exports for the live Tadoss instance supplied on stdin."""

import argparse
import base64
import binascii
import json
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

OAUTH_FIELDS = {
    "client_id": "TADOSS_OAUTH_CLIENT_ID",
    "client_secret": "TADOSS_OAUTH_CLIENT_SECRET",
    "token_endpoint": "TADOSS_OAUTH_TOKEN_URL",
    "scopes": "TADOSS_OAUTH_SCOPES",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument(
        "--auth-method", choices=["auto", "oauth", "query"], default="auto"
    )
    parser.add_argument("--api-url", default="")
    args = parser.parse_args()
    try:
        instance = json.load(sys.stdin)
        if instance.get("kind") == "List":
            items = instance.get("items", [])
            if len(items) != 1:
                parser.exit(
                    1, "Select exactly one rendered Tadoss name with INSTANCE=<name>\n"
                )
            instance = items[0]
        if instance.get("kind") != "Tadoss":
            raise ValueError
        namespace = instance["metadata"]["namespace"]
        endpoint = args.api_url or instance["status"]["endpoint"]
        auth = instance.get("spec", {}).get("auth", {})
        oauth = auth.get("oauth2", {})
        method = args.auth_method
        if method == "auto":
            method = "oauth" if oauth.get("issuer") else "query"
        if method == "oauth":
            secret = oauth.get("clientCredentialsSecret")
            if not oauth.get("issuer") or not secret:
                parser.exit(
                    1, "OAuth access requires auth.oauth2.clientCredentialsSecret\n"
                )
        else:
            if auth.get("tokenEnabled") is False:
                parser.exit(
                    1, "Query token authentication is disabled for this instance\n"
                )
            secret = instance["status"]["tokenSecret"]
        if not all((namespace, endpoint, secret)):
            raise ValueError
    except (KeyError, ValueError):
        parser.exit(1, "Instance access details are unavailable; check env:status\n")

    kubeconfig = str(args.kubeconfig.resolve())
    result = subprocess.run(  # noqa: S603 fixed executable, argv only
        [  # noqa: S607 developer tool from PATH
            "kubectl",
            "--kubeconfig",
            kubeconfig,
            "--request-timeout=30s",
            "-n",
            namespace,
            "get",
            "secret",
            secret,
            "-o",
            "jsonpath={.data}",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        parser.exit(1, "Cannot read the access Secret; check access and env:status\n")
    fields = OAUTH_FIELDS if method == "oauth" else {"token": "TADOSS_API_TOKEN"}
    values = {
        "KUBECONFIG": kubeconfig,
        "TADOSS_URL": endpoint,
        "TADOSS_AUTH_METHOD": method,
    }
    try:
        data = json.loads(result.stdout)
        for key, variable in fields.items():
            values[variable] = base64.b64decode(data[key], validate=True).decode(
                "utf-8"
            )
    except (KeyError, ValueError, binascii.Error, UnicodeError):
        parser.exit(1, "Access Secret is missing required UTF-8 credential fields\n")
    if any(not value or "\0" in value for value in values.values()):
        parser.exit(1, "Instance access details must be nonempty and contain no NUL\n")
    if method == "oauth":
        url = urlsplit(values["TADOSS_OAUTH_TOKEN_URL"])
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.fragment
        ):
            parser.exit(
                1, "OAuth token endpoint must use HTTPS without embedded credentials\n"
            )
        values["TADOSS_OAUTH_CLIENT_AUTH_METHOD"] = "post"
        hint = "OAuth client credentials (client authentication: POST body)"
        clear = ["TADOSS_API_TOKEN"]
    else:
        hint = "URL access token (query parameter: access_token)"
        clear = [*OAUTH_FIELDS.values(), "TADOSS_OAUTH_CLIENT_AUTH_METHOD"]
    print("# Authentication: " + hint)
    print("unset " + " ".join(clear))
    print(
        "\n".join(f"export {key}={shlex.quote(value)}" for key, value in values.items())
    )


if __name__ == "__main__":
    main()
