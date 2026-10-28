"""Install synthetic OAuth and webhook fixtures only in disposable namespaces."""

import base64
import json
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def install(cluster):
    args, cr = cluster.args, cluster.guard()
    parsed = urlsplit(args.fixture_url or "")
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("fixture requires --fixture-url https://<ingress-host>[:port]")
    name, ns = "qualification", args.namespace
    metadata = {"name": name, "namespace": ns}
    internal = f"{name}.{ns}.svc"
    image = (
        cr["spec"].get("api", {}).get("image") or cr["status"]["resolved"]["apiImage"]
    )

    def create(kind, spec, version="v1", **fields):
        cluster.kubectl(
            "create",
            "-f",
            "-",
            body=json.dumps(
                {
                    "apiVersion": version,
                    "kind": kind,
                    "metadata": metadata,
                    **spec,
                    **fields,
                }
            ),
        )

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    create(
        "Secret",
        {
            "stringData": {
                "key.pem": key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ).decode(),
                "client_secret": secrets.token_urlsafe(32),
                "webhook_token": secrets.token_urlsafe(32),
                "client_id": "qualification",
                "token_endpoint": args.fixture_url.rstrip("/") + "/token",
                "scopes": "tads-api/read tads-api/write tads-api/delete tads-api/admin",
            }
        },
    )
    create(
        "Certificate",
        {
            "spec": {
                "secretName": "qualification-tls",
                "dnsNames": [parsed.hostname, internal],
                "issuerRef": {"name": args.fixture_issuer, "kind": "ClusterIssuer"},
            }
        },
        "cert-manager.io/v1",
    )
    cluster.kubectl(
        "wait", "certificate/qualification", "--for=condition=Ready", "--timeout=120s"
    )
    certificate = cluster.get("secret", "qualification-tls")["data"]
    ca = base64.b64decode(certificate.get("ca.crt", certificate["tls.crt"])).decode()
    source = (Path(__file__).parent / "fixture_server.py").read_text()
    create("ConfigMap", {"data": {"server.py": source, "ca.pem": ca}})
    labels = {"app": name}
    create(
        "Deployment",
        {
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "containers": [
                            {
                                "name": "fixture",
                                "image": image,
                                "command": ["python", "/fixture/server.py"],
                                "env": [
                                    {
                                        "name": "ISSUER",
                                        "value": args.fixture_url.rstrip("/"),
                                    }
                                ],
                                "ports": [{"name": "https", "containerPort": 443}],
                                "resources": {
                                    "requests": {"cpu": "100m", "memory": "128Mi"},
                                    "limits": {"cpu": "1", "memory": "256Mi"},
                                },
                                "readinessProbe": {
                                    "httpGet": {
                                        "path": "/jwks",
                                        "port": "https",
                                        "scheme": "HTTPS",
                                    }
                                },
                                "volumeMounts": [
                                    {
                                        "name": "source",
                                        "mountPath": "/fixture",
                                        "readOnly": True,
                                    },
                                    {
                                        "name": "credentials",
                                        "mountPath": "/credentials",
                                        "readOnly": True,
                                    },
                                    {
                                        "name": "tls",
                                        "mountPath": "/tls",
                                        "readOnly": True,
                                    },
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "source", "configMap": {"name": name}},
                            {"name": "credentials", "secret": {"secretName": name}},
                            {
                                "name": "tls",
                                "secret": {"secretName": "qualification-tls"},
                            },
                        ],
                    },
                },
            }
        },
        "apps/v1",
    )
    create(
        "Service",
        {
            "spec": {
                "selector": labels,
                "ports": [{"name": "https", "port": 443, "targetPort": "https"}],
            }
        },
    )
    create(
        "Ingress",
        {
            "metadata": {
                **metadata,
                "annotations": {
                    "traefik.ingress.kubernetes.io/service.serversscheme": "https"
                },
            },
            "spec": {
                "ingressClassName": "traefik",
                "tls": [
                    {"hosts": [parsed.hostname], "secretName": "qualification-tls"}
                ],
                "rules": [
                    {
                        "host": parsed.hostname,
                        "http": {
                            "paths": [
                                {
                                    "path": "/",
                                    "pathType": "Prefix",
                                    "backend": {
                                        "service": {
                                            "name": name,
                                            "port": {"number": 443},
                                        }
                                    },
                                }
                            ]
                        },
                    }
                ],
            },
        },
        "networking.k8s.io/v1",
    )
    # Traefik must trust the fixture CA on its backend connection as well.
    cluster.kubectl(
        "create",
        "-f",
        "-",
        body=json.dumps(
            {
                "apiVersion": "traefik.io/v1alpha1",
                "kind": "ServersTransport",
                "metadata": metadata,
                "spec": {
                    "serverName": internal,
                    "rootCAsSecrets": ["qualification-tls"],
                },
            }
        ),
    )
    cluster.kubectl(
        "annotate",
        "service",
        name,
        f"traefik.ingress.kubernetes.io/service.serverstransport={ns}-qualification@kubernetescrd",
    )
    patch = {
        "auth": {
            "tokenEnabled": False,
            "oauth2": {
                "issuer": args.fixture_url.rstrip("/"),
                "audience": "qualification",
                "jwksURI": f"https://{internal}/jwks",
                "clientCredentialsSecret": name,
            },
        }
    }
    for component in ["api", "worker"]:
        previous = cr["spec"].get(component, {})
        patch[component] = {
            "env": {"SSL_CERT_FILE": "/fixture/ca.pem"},
            "volumes": [
                *previous.get("volumes", []),
                {"name": "qualification-ca", "configMap": {"name": name}},
            ],
            "volumeMounts": [
                *previous.get("volumeMounts", []),
                {"name": "qualification-ca", "mountPath": "/fixture", "readOnly": True},
            ],
        }
    patch["worker"]["env"]["TADOSS_WEBHOOK_ALLOWED_HOSTS"] = internal
    cluster.patch_instance(patch)
    api_tls = cluster.get("ingress", args.instance + "-api")["spec"]["tls"][0][
        "secretName"
    ]
    api_cert = cluster.get("secret", api_tls)["data"]
    bundle = ca + base64.b64decode(api_cert.get("ca.crt", api_cert["tls.crt"])).decode()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "ca.pem").write_text(bundle)
    print(
        f"Fixture installed; trust {args.output / 'ca.pem'}. "
        "Synthetic OAuth measures JWT verification, not identity-provider capacity."
    )
