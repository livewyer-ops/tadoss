"""Task entry points for platform, operator and instance deployment."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE = os.getenv("TADOSS_PROFILE", "local-kind")
CLUSTER = os.getenv("TADOSS_CLUSTER", "tadoss")
ENVIRONMENT = os.getenv("TADOSS_ENVIRONMENT", PROFILE)
KUBECONFIG = Path(
    os.getenv("TADOSS_KUBECONFIG", f".local/{CLUSTER}.kubeconfig")
).resolve()


def run(*args, capture=False, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=capture, **kwargs)


def kubectl(*args, **kwargs):
    return run("kubectl", "--kubeconfig", str(KUBECONFIG), *args, **kwargs)


def image_tag():
    digest = hashlib.sha256()
    paths = [ROOT / name for name in ("Dockerfile", "pyproject.toml", "uv.lock")]
    paths += list((ROOT / "tadoss").rglob("*.py"))
    paths += list((ROOT / "tadoss/contract").glob("*"))
    paths += [
        path
        for path in (ROOT / "operator").rglob("*")
        if path.is_file()
        and (path.suffix == ".go" or path.name in {"go.mod", "go.sum", "Dockerfile"})
    ]
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes())
    return "dev-" + digest.hexdigest()[:12]


def apply_environment(tag=None):
    environment = ROOT / "deploy/environments" / ENVIRONMENT
    if not environment.is_dir():
        raise SystemExit(f"Unknown environment: {ENVIRONMENT}")
    run(
        "helmfile",
        "--kubeconfig",
        str(KUBECONFIG),
        "--file",
        str(ROOT / "deploy/platform/helmfile.yaml.gotmpl"),
        "--state-values-file",
        str(ROOT / "deploy/platform/values/defaults.yaml"),
        "--state-values-file",
        str(environment / "platform-values.yaml"),
        "sync",
    )
    # Helm installs CRDs on first install but does not upgrade them.
    kubectl(
        "apply", "--server-side", "-f", str(ROOT / "deploy/charts/tadoss-operator/crds")
    )
    image_args = (
        ["--set", "image.repository=livewyer/tadoss-operator,image.tag=" + tag]
        if tag
        else []
    )
    run(
        "helm",
        "--kubeconfig",
        str(KUBECONFIG),
        "upgrade",
        "--install",
        "tadoss-operator",
        str(ROOT / "deploy/charts/tadoss-operator"),
        "--namespace",
        "tadoss-system",
        "--create-namespace",
        "--wait",
        "--timeout",
        "5m",
        "--values",
        str(environment / "operator-values.yaml"),
        *image_args,
    )
    rendered = kubectl("kustomize", str(environment), capture=True).stdout
    if tag:
        local = ROOT / ".local" / "instance"
        local.mkdir(parents=True, exist_ok=True)
        (local / "resources.yaml").write_text(rendered)
        (local / "kustomization.yaml").write_text(
            "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
            "resources: [resources.yaml]\npatches:\n"
            "  - target:\n      kind: Tadoss\n    patch: |-\n"
            "      - op: add\n        path: /spec/image\n"
            "        value: livewyer/tadoss-api:" + tag + "\n"
        )
        rendered = kubectl("kustomize", str(local), capture=True).stdout
    kubectl("apply", "-f", "-", input=rendered)
    wait()


def wait():
    generation = kubectl(
        "-n",
        "tads",
        "get",
        "tadoss/tadoss",
        "-o",
        "jsonpath={.metadata.generation}",
        capture=True,
    ).stdout
    kubectl(
        "-n",
        "tads",
        "wait",
        "tadoss/tadoss",
        f"--for=jsonpath={{.status.observedGeneration}}={generation}",
        "--timeout=10m",
    )
    kubectl(
        "-n", "tads", "wait", "tadoss/tadoss", "--for=condition=Ready", "--timeout=10m"
    )


def summary():
    result = json.loads(
        kubectl("-n", "tads", "get", "tadoss/tadoss", "-o", "json", capture=True).stdout
    )
    print(
        json.dumps(
            {
                "endpoint": result["status"].get("endpoint"),
                "token_secret": result["status"].get("tokenSecret"),
                "kubeconfig": str(KUBECONFIG),
            },
            indent=2,
        )
    )
    if ENVIRONMENT in {"local-kind", "edge", "single-server", "multi-server"}:
        print("Kind API docs: https://api.tadoss.localtest.me:18443/docs")


def kind_up():
    KUBECONFIG.parent.mkdir(parents=True, exist_ok=True)
    config = (
        ROOT
        / "deploy/kind"
        / ("multi-node.yaml" if PROFILE == "multi-server" else "single-node.yaml")
    )
    clusters = run("kind", "get", "clusters", capture=True).stdout.splitlines()
    if CLUSTER not in clusters:
        run(
            "kind",
            "create",
            "cluster",
            "--name",
            CLUSTER,
            "--config",
            str(config),
            "--kubeconfig",
            str(KUBECONFIG),
        )
    else:
        run(
            "kind",
            "export",
            "kubeconfig",
            "--name",
            CLUSTER,
            "--kubeconfig",
            str(KUBECONFIG),
        )
    if PROFILE == "multi-server":
        nodes = json.loads(kubectl("get", "nodes", "-o", "json", capture=True).stdout)[
            "items"
        ]
        if len(nodes) < 4:
            raise SystemExit(
                "multi-server needs a control plane and three workers; "
                "recreate this Kind cluster"
            )
    tag = image_tag()
    images = ["livewyer/tadoss-api:" + tag, "livewyer/tadoss-operator:" + tag]
    run("docker", "build", "-t", images[0], str(ROOT))
    run("docker", "build", "-t", images[1], str(ROOT / "operator"))
    run("kind", "load", "docker-image", "--name", CLUSTER, *images)
    apply_environment(tag)
    summary()


def env_init():
    name = os.environ["TADOSS_NEW_ENVIRONMENT"]
    domain = os.environ["TADOSS_DOMAIN"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", name) or not re.fullmatch(
        r"[a-z0-9.-]+", domain
    ):
        raise SystemExit("Use a DNS-style environment name and domain")
    destination = ROOT / "deploy/environments" / name
    shutil.copytree(ROOT / "deploy/environments" / PROFILE, destination)
    patch = destination / "tadoss-patch.yaml"
    patch.write_text(
        patch.read_text()
        .replace("api.tadoss.localtest.me", domain)
        .replace("tadoss-selfsigned", "tadoss-public")
    )
    (destination / "platform-values.yaml").write_text(
        'tls:\n  mode: acme\n  issuerName: tadoss-public\n  email: ""\n'
    )
    print(f"Created {destination}; set tls.email and review platform values")


def main():
    if PROFILE not in {"local-kind", "edge", "single-server", "multi-server"}:
        raise SystemExit("Unknown profile")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", ENVIRONMENT) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,39}", CLUSTER
    ):
        raise SystemExit("Use DNS-style cluster and environment names")
    command = sys.argv[1]
    if command == "kind-up":
        kind_up()
    elif command == "env-init":
        env_init()
    elif command == "env-apply":
        apply_environment()
    elif command == "wait":
        wait()
    elif command == "summary":
        summary()
    elif command == "kind-down":
        run("kind", "delete", "cluster", "--name", CLUSTER)
        KUBECONFIG.unlink(missing_ok=True)
    elif command == "chainsaw":
        report_path = ROOT / "reports" / PROFILE
        report_path.mkdir(parents=True, exist_ok=True)
        deployed = json.loads(
            kubectl(
                "-n", "tads", "get", "tadoss/tadoss", "-o", "json", capture=True
            ).stdout
        )
        run(
            "chainsaw",
            "test",
            "--config",
            str(ROOT / "operator/test/chainsaw/configuration.yaml"),
            "--test-dir",
            str(ROOT / "operator/test/chainsaw"),
            "--report-path",
            str(report_path),
            "--set-string",
            "apiImage=" + deployed["spec"]["image"],
            env={**os.environ, "KUBECONFIG": str(KUBECONFIG)},
        )
    else:
        raise SystemExit("Unknown deployment command")


if __name__ == "__main__":
    main()
