# ruff: noqa: S101
"""Exercise a deployed API through an explicit kubeconfig and port-forward."""

import argparse
import base64
import http.client
import json
import socket
import ssl
import subprocess
import time
from contextlib import closing
from uuid import uuid4

import httpx


def check_https(read, instance, port):
    ingress = read("ingress", instance + "-api")
    host = ingress["spec"]["rules"][0]["host"]
    secret_name = ingress["spec"]["tls"][0]["secretName"]
    for _ in range(60):
        try:
            secret = read("secret", secret_name)["data"]
            certificate = base64.b64decode(secret.get("ca.crt", secret["tls.crt"]))
            context = ssl.create_default_context(cadata=certificate.decode())
            with (
                socket.create_connection(("127.0.0.1", port), timeout=5) as connection,
                context.wrap_socket(connection, server_hostname=host) as tls,
            ):
                if not tls.getpeercert().get("issuer"):
                    raise ssl.SSLCertVerificationError("Empty certificate issuer")
                tls.sendall(
                    (
                        f"GET /readyz HTTP/1.1\r\nHost: {host}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode()
                )
                with http.client.HTTPResponse(tls) as response:
                    response.begin()
                    if response.status == 200:
                        print("HTTPS ingress and certificate verification passed")
                        return
        except (OSError, subprocess.CalledProcessError, http.client.HTTPException):
            pass
        time.sleep(2)
    raise RuntimeError("HTTPS ingress did not serve readiness with a valid certificate")


def check_database_tls(kubectl, instance):
    subprocess.run(  # noqa: S603 fixed executable and checked-in Python only
        [
            *kubectl,
            "exec",
            "deployment/" + instance + "-api",
            "--",
            "python",
            "-c",
            """
import socket
import psycopg
from psycopg.conninfo import conninfo_to_dict
from tadoss.settings import Settings
settings = Settings.load()
uri = settings.database_url()
parameters = conninfo_to_dict(uri)
assert parameters['sslmode'] == 'verify-full'
with psycopg.connect(uri, connect_timeout=5) as connection:
    query = 'SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()'
    assert connection.execute(query).fetchone()[0]
addresses = socket.getaddrinfo(
    parameters['host'], int(parameters.get('port', 5432)), type=socket.SOCK_STREAM
)
address = addresses[0][4][0]
try:
    psycopg.connect(
        uri, host='wrong-database.invalid', hostaddr=address, connect_timeout=5
    )
except psycopg.OperationalError:
    print('Managed PostgreSQL TLS and hostname rejection passed')
else:
    raise SystemExit('Incorrect database hostname was accepted')
""",
        ],
        check=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--namespace", default="tads")
    parser.add_argument("--instance", default="tadoss")
    parser.add_argument("--expected-image", default="")
    parser.add_argument("--https-port", type=int, default=0)
    args = parser.parse_args()
    kubectl = ["kubectl", "--kubeconfig", args.kubeconfig, "-n", args.namespace]

    def read(resource, name):
        return json.loads(
            subprocess.check_output(  # noqa: S603 fixed executable, argv only
                [*kubectl, "get", resource, name, "-o", "json"]
            )
        )

    instance = read("tadoss", args.instance)
    assert instance["status"]["phase"] == "Ready"
    assert (
        instance["status"]["observedGeneration"] == instance["metadata"]["generation"]
    )
    if (
        instance["spec"].get("backends", {}).get("db", {}).get("providedBy", "cnpg")
        == "cnpg"
    ):
        check_database_tls(kubectl, args.instance)
    if args.https_port:
        check_https(read, args.instance, args.https_port)
    if args.expected_image:
        for component in ("api", "worker"):
            deployment = read("deployment", args.instance + "-" + component)
            if (
                deployment["spec"]["template"]["spec"]["containers"][0]["image"]
                != args.expected_image
            ):
                raise AssertionError(
                    "Release catalogue did not select the qualified image"
                )
    secret = read("secret", instance["status"]["tokenSecret"])
    token = base64.b64decode(secret["data"]["token"]).decode()
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    forward = subprocess.Popen(  # noqa: S603 fixed executable, argv only
        [*kubectl, "port-forward", "service/" + args.instance + "-api", f"{port}:8000"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            for _ in range(60):
                try:
                    if client.get("/readyz").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("API did not become reachable")
            assert client.get("/tracks").status_code == 401

            def request(method, path, body=None, expected=200):
                response = client.request(
                    method, path, params={"access_token": token}, json=body
                )
                if response.status_code != expected:
                    raise AssertionError(
                        f"{method} {path}: {response.status_code}, expected {expected}"
                    )
                assert response.headers.get("X-Request-ID")
                return response

            schema, track = str(uuid4()), str(uuid4())
            request(
                "PUT",
                "/schemas/" + schema,
                {"id": schema, "schema": {"type": "object"}},
                201,
            )
            request(
                "PUT",
                "/tracks/" + track,
                {
                    "id": track,
                    "schema_id": schema,
                    "timeline_reference": {"entity_id": "urn:tadoss:qualification"},
                    "editable_events": True,
                },
                201,
            )
            path = "/tracks/" + track + "/events"
            payload = [
                {
                    "id": str(uuid4()),
                    "timerange": f"[{n}:0_{n + 1}:0)",
                    "payload": {"index": n},
                }
                for n in range(1001)
            ]
            for offset in range(0, len(payload), 500):
                result = request("POST", path, payload[offset : offset + 500]).json()
                assert all(
                    item["http_status_code"] == 201 for item in result["responses"]
                )
            response = request("DELETE", path, expected=202)
            location = response.headers["Location"]
            for _ in range(120):
                state = request("GET", location).json()["status"]
                if state == "done":
                    break
                assert state != "error"
                time.sleep(1)
            else:
                raise RuntimeError("Worker did not complete deferred deletion")
            assert request("GET", path).json() == []
            request("DELETE", "/tracks/" + track, expected=204)
            request("DELETE", "/schemas/" + schema, expected=204)
            print("Deployed authentication, event writes and worker deletion passed")
    finally:
        forward.terminate()
        try:
            forward.wait(timeout=10)
        except subprocess.TimeoutExpired:
            forward.kill()
            forward.wait()


if __name__ == "__main__":
    main()
