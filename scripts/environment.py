"""Environment composition and installation commands used by Task and CI."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

import yaml
from tadoss import __version__

ROOT = Path(__file__).resolve().parents[1]
PROFILES = yaml.safe_load((ROOT / "deploy/profiles.yaml").read_text())


def write_yaml(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False))


def kustomization(resources, **extra):
    return {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": resources,
        **extra,
    }


def image_override(image):
    """Parse an explicit Docker tag or SHA256 digest for Kustomize."""
    name, at, digest = image.partition("@")
    repository, colon, tag = name.rpartition(":")
    tagged = bool(colon and "/" not in tag)
    if tagged:
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
            raise ValueError("Image tag is invalid")
        name = repository
    if at:
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise ValueError("Image digest must be sha256 followed by 64 hex digits")
    elif not tagged:
        raise ValueError("Image must have an explicit tag or digest")
    component = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
    registry = r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?"
    if not re.fullmatch(rf"(?:{registry}/)?{component}(?:/{component})*", name):
        raise ValueError("Image repository is invalid")
    if ":" in name.split("/", 1)[0]:
        port = int(name.split("/", 1)[0].rsplit(":", 1)[1])
        if not 0 < port <= 65535:
            raise ValueError("Image registry port is invalid")
    return {"newName": name, **({"digest": digest} if at else {"newTag": tag})}


def init_instance(directory, name, namespace, version, image=None):
    for value in (name, namespace):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,38}[a-z0-9]|[a-z]", value):
            raise ValueError(
                "Instance and namespace must be DNS labels of at most 40 characters"
            )
    target = directory / "instances" / name
    if target.exists():
        raise ValueError(f"Instance already exists: {target}")
    spec = {"version": version}
    if image:
        image_override(image)
        spec["api"] = {"image": image}
    write_yaml(
        target / "namespace.yaml",
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}},
    )
    write_yaml(
        target / "tadoss.yaml",
        {
            "apiVersion": "tadoss.livewyer.io/v1alpha1",
            "kind": "Tadoss",
            "metadata": {"name": name, "namespace": namespace},
            "spec": spec,
        },
    )
    write_yaml(
        target / "kustomization.yaml", kustomization(["namespace.yaml", "tadoss.yaml"])
    )
    write_yaml(
        directory / "kustomization.yaml",
        kustomization(
            [
                str(p.relative_to(directory))
                for p in sorted((directory / "instances").iterdir())
                if p.is_dir()
            ]
        ),
    )


def init_environment(
    directory,
    profile,
    domain,
    version,
    instance="tadoss",
    namespace="tads",
    image=None,
    operator_image=None,
):
    if directory.exists():
        raise ValueError(f"Environment already exists: {directory}")
    if profile not in PROFILES:
        raise ValueError("Unknown profile")
    if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", domain):
        raise ValueError("Invalid base domain")
    replacement = image_override(operator_image) if operator_image else None
    if image:
        image_override(image)
    values = {"tls": {"mode": "selfSigned" if profile == "local-kind" else "existing"}}
    if profile == "local-kind":
        values["platform"] = {
            "traefik": {
                "values": {
                    "service": {"type": "NodePort"},
                    "ports": {
                        "web": {"nodePort": 30080},
                        "websecure": {"nodePort": 30443},
                    },
                }
            }
        }
    write_yaml(directory / "platform-values.yaml", values)
    defaults = {"profile": profile, "baseDomain": domain, "ingressClassName": "traefik"}
    if profile == "local-kind":
        defaults["clusterIssuer"] = "tadoss-selfsigned"
    write_yaml(directory / "operator/defaults.yaml", defaults)
    patch = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "tadoss-operator", "namespace": "tadoss-system"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "manager",
                            "env": [
                                {
                                    "name": "TADOSS_INSTANCE_DEFAULTS",
                                    "value": "/etc/tadoss/defaults/defaults.yaml",
                                }
                            ],
                            "volumeMounts": [
                                {
                                    "name": "defaults",
                                    "mountPath": "/etc/tadoss/defaults",
                                    "readOnly": True,
                                }
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "defaults",
                            "configMap": {"name": "tadoss-instance-defaults"},
                        }
                    ],
                }
            }
        },
    }
    overlay = kustomization(
        [os.path.relpath(ROOT / "deploy/operator", directory / "operator")],
        configMapGenerator=[
            {
                "name": "tadoss-instance-defaults",
                "namespace": "tadoss-system",
                "files": ["defaults.yaml"],
                "options": {
                    "immutable": True,
                    "labels": {
                        "app.kubernetes.io/name": "tadoss-instance-defaults",
                        "app.kubernetes.io/managed-by": "kustomize",
                    },
                },
            }
        ],
        patches=[{"patch": yaml.safe_dump(patch, sort_keys=False)}],
    )
    if replacement:
        overlay["images"] = [{"name": "livewyer/tadoss-operator", **replacement}]
    write_yaml(directory / "operator/kustomization.yaml", overlay)
    init_instance(directory, instance, namespace, version, image)


def run(arguments, capture=False, allowed_codes=(0,)):
    result = subprocess.run(  # noqa: S603  fixed executables and argv, no shell
        arguments, cwd=ROOT, check=False, text=True, capture_output=capture
    )
    if result.returncode not in allowed_codes:
        raise RuntimeError(f"{arguments[0]} failed with exit code {result.returncode}")
    return result


def instances(directory, selection):
    paths = (
        [directory / "instances" / selection]
        if selection
        else sorted((directory / "instances").iterdir())
    )
    result = []
    for path in paths:
        resource = yaml.safe_load((path / "tadoss.yaml").read_text())
        result.append(
            (path, resource["metadata"]["namespace"], resource["metadata"]["name"])
        )
    return result


def operate(command, directory, kubeconfig, selection=None, strict=False, timeout=600):
    directory, kubeconfig = directory.resolve(), kubeconfig.resolve()
    kubectl = ["kubectl", "--kubeconfig", str(kubeconfig)]
    platform = [
        "helmfile",
        "--file",
        str(ROOT / "deploy/platform/helmfile.yaml.gotmpl"),
        "--kubeconfig",
        str(kubeconfig),
        "--state-values-file",
        str(directory / "platform-values.yaml"),
    ]
    target = directory / "instances" / selection if selection else directory
    if command == "apply":
        run([*platform, "sync"])
        run([*kubectl, "apply", "--server-side", "-k", str(directory / "operator")])
        run(
            [
                *kubectl,
                "rollout",
                "status",
                "deployment/tadoss-operator",
                "-n",
                "tadoss-system",
                f"--timeout={timeout}s",
            ]
        )
        run(
            [
                *kubectl,
                "wait",
                "--for=condition=Established",
                "crd/tadosses.tadoss.livewyer.io",
                f"--timeout={timeout}s",
            ]
        )
        run([*kubectl, "apply", "--server-side", "-k", str(target)])
    elif command == "instance-apply":
        run([*kubectl, "apply", "--server-side", "-k", str(target)])
    elif command == "diff":
        run(
            [*platform, "diff", "--detailed-exitcode"],
            allowed_codes=(0,) if strict else (0, 2),
        )
        for path in (directory / "operator", target):
            run(
                [*kubectl, "diff", "--server-side", "-k", str(path)],
                allowed_codes=(0,) if strict else (0, 1),
            )
    elif command in {"status", "summary", "wait", "credentials"}:
        revision = hashlib.sha256(
            (directory / "operator/defaults.yaml").read_bytes()
        ).hexdigest()
        for _, namespace, name in instances(directory, selection):
            if command == "wait":
                deadline = time.monotonic() + timeout
                while True:
                    state = json.loads(
                        run(
                            [
                                *kubectl,
                                "get",
                                "tadoss",
                                name,
                                "-n",
                                namespace,
                                "-o",
                                "json",
                            ],
                            True,
                        ).stdout
                    )
                    status = state.get("status", {})
                    if status.get("phase") == "Error":
                        raise RuntimeError(
                            f"{namespace}/{name}: {status.get('conditions', [])}"
                        )
                    if (
                        status.get("phase") == "Ready"
                        and status.get("observedGeneration")
                        == state["metadata"]["generation"]
                        and status.get("appliedDefaultsRevision") == revision
                    ):
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for {namespace}/{name}")
                    time.sleep(2)
            elif command == "credentials":
                # Only this explicit command reads credential values.
                state = json.loads(
                    run(
                        [
                            *kubectl,
                            "get",
                            "tadoss",
                            name,
                            "-n",
                            namespace,
                            "-o",
                            "json",
                        ],
                        True,
                    ).stdout
                )
                secret = state["status"]["tokenSecret"]
                run(
                    [
                        *kubectl,
                        "get",
                        "secret",
                        secret,
                        "-n",
                        namespace,
                        "-o",
                        "jsonpath={.data.token}",
                    ]
                )
                print("\n(base64-encoded API token)")
            else:
                run(
                    [
                        *kubectl,
                        "get",
                        "tadoss",
                        name,
                        "-n",
                        namespace,
                        "-o",
                        "json" if command == "status" else "wide",
                    ]
                )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "init",
            "instance-init",
            "apply",
            "instance-apply",
            "diff",
            "status",
            "summary",
            "wait",
            "credentials",
        ],
    )
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--profile", choices=list(PROFILES), default="local-kind")
    parser.add_argument("--domain", default="tadoss.localtest.me")
    parser.add_argument("--version", default=__version__)
    parser.add_argument("--instance")
    parser.add_argument("--namespace", default="tads")
    parser.add_argument("--image")
    parser.add_argument("--operator-image")
    parser.add_argument(
        "--kubeconfig", type=Path, default=ROOT / ".local/tadoss.kubeconfig"
    )
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    directory = args.directory.resolve()
    try:
        if args.command == "init":
            init_environment(
                directory,
                args.profile,
                args.domain,
                args.version,
                args.instance or "tadoss",
                args.namespace,
                args.image,
                args.operator_image,
            )
        elif args.command == "instance-init":
            init_instance(
                directory,
                args.instance or "tadoss",
                args.namespace,
                args.version,
                args.image,
            )
        else:
            operate(
                args.command,
                directory,
                args.kubeconfig,
                args.instance,
                args.strict,
                args.timeout,
            )
    except (ValueError, RuntimeError, TimeoutError, OSError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
