# ruff: noqa: S603, S607
"""Qualify an explicitly disposable TADOSS namespace over verified HTTPS/OAuth."""

import argparse
import base64
import copy
import hashlib
import json
import os
import platform
import re
import ssl
import subprocess
import threading
import time
from itertools import pairwise
from pathlib import Path
from uuid import uuid4

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "scripts/qualification"
DISPOSABLE = "qualification.tadoss.livewyer.io/disposable"
K6_IMAGE = (
    "grafana/k6:1.6.1@sha256:"
    "a5ad6bc089a08d77c3ec49f3db8c6fa7a148e4073efcac44c675dbaf3568d8e1"
)


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def event_id(track, index):
    return track[:24] + f"{index:012x}"


def event(track, index, payload_bytes):
    return {
        "id": event_id(track, index),
        "timerange": f"[{index}:0_{index + 1}:0)",
        "payload": {"text": "x" * payload_bytes},
    }


def redact(value):
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, dict):
        if (
            "name" in value
            and "value" in value
            and re.search(
                r"TOKEN|PASSWORD|SECRET|CREDENTIAL|DATABASE$", value["name"], re.I
            )
        ):
            return {**value, "value": "[redacted]"}
        return {
            k: "[redacted]"
            if re.search(
                r"password|client.secret|api.token|metrics.token|api_key_value", k, re.I
            )
            else redact(v)
            for k, v in value.items()
            if k != "managedFields"
        }
    return value


class Cluster:
    def __init__(self, args):
        self.args = args
        self.prefix = [
            "kubectl",
            "--kubeconfig",
            str(args.kubeconfig),
            "--request-timeout=30s",
        ]

    def kubectl(self, *command, body=None):
        return subprocess.check_output(
            [*self.prefix, "-n", self.args.namespace, *command],
            input=body,
            text=True,
            stderr=subprocess.PIPE,
        )

    def get(self, kind, name=None):
        return json.loads(
            self.kubectl("get", kind, *([name] if name else []), "-o", "json")
        )

    def guard(self):
        ns = self.get("namespace", self.args.namespace)
        if ns["metadata"].get("labels", {}).get(DISPOSABLE) != "true":
            raise ValueError(
                "Namespace is not marked disposable; use prepare with a new namespace"
            )
        cr = self.get("tadoss", self.args.instance)
        if cr["metadata"].get("labels", {}).get(DISPOSABLE) != "true":
            raise ValueError("Instance is not explicitly marked disposable")
        return cr

    def patch_instance(self, patch):
        self.guard()
        self.kubectl(
            "patch",
            "tadoss",
            self.args.instance,
            "--type=merge",
            "-p",
            json.dumps({"spec": patch}),
        )
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            cr = self.get("tadoss", self.args.instance)
            if cr.get("status", {}).get("observedGeneration") == cr["metadata"][
                "generation"
            ] and any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in cr.get("status", {}).get("conditions", [])
            ):
                return
            time.sleep(2)
        raise TimeoutError(
            "Instance did not converge after changing qualification settings"
        )

    def sql(self, query):
        db = self.get("cluster", self.args.instance + "-db")
        primary = db["status"]["currentPrimary"]
        raw = self.kubectl(
            "exec",
            primary,
            "-c",
            "postgres",
            "--",
            "psql",
            "-XAt",
            "-U",
            "postgres",
            "-d",
            "tadoss",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            query,
        )
        return raw

    def database(self):
        return json.loads(self.sql((ASSETS / "snapshot.sql").read_text()))

    def snapshot(self, path):
        data = {
            kind: self.get(kind)
            for kind in [
                "tadoss",
                "deployments",
                "pods",
                "hpa",
                "clusters",
                "poolers",
                "pvc",
                "events",
            ]
        }
        data["nodes"] = self.get("nodes")
        data["storageclasses"] = self.get("storageclasses")
        data["driver"] = {
            "architecture": platform.machine(),
            "platform": platform.platform(),
        }
        data["git"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        data["dirty"] = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True
            )
        )
        digest = hashlib.sha256()
        for source in sorted((ROOT / "src/app").rglob("*.py")):
            digest.update(str(source.relative_to(ROOT)).encode())
            digest.update(source.read_bytes())
        data["source_sha256"] = digest.hexdigest()
        save(path, redact(data))


class API:
    def __init__(self, cluster, cr):
        args = cluster.args
        oauth = cr["spec"].get("auth", {}).get("oauth2", {})
        if not oauth.get("issuer") or not oauth.get("clientCredentialsSecret"):
            raise ValueError(
                "Qualification requires configured OAuth with clientCredentialsSecret"
            )
        data = cluster.get("secret", oauth["clientCredentialsSecret"])["data"]
        self.credentials = {k: base64.b64decode(v).decode() for k, v in data.items()}
        self.url = (args.api_url or cr["status"]["endpoint"]).rstrip("/")
        if not self.url.startswith("https://") or not self.credentials[
            "token_endpoint"
        ].startswith("https://"):
            raise ValueError("API and OAuth must use verified HTTPS")
        self.client = httpx.Client(
            verify=ssl.create_default_context(cafile=args.ca_file), timeout=35
        )
        self.token, self.expires = "", 0

    def request(self, method, path, **kwargs):
        if time.monotonic() >= self.expires:
            response = self.client.post(
                self.credentials["token_endpoint"],
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.credentials["client_id"],
                    "client_secret": self.credentials["client_secret"],
                    "scope": self.credentials["scopes"],
                },
            )
            response.raise_for_status()
            issued = response.json()
            self.token = issued["access_token"]
            self.expires = time.monotonic() + max(1, issued.get("expires_in", 300) - 30)
        # No write retries: a missing response can follow a committed transaction.
        return self.client.request(
            method,
            self.url + path,
            headers={"Authorization": "Bearer " + self.token},
            **kwargs,
        )


def prepare(cluster):
    args = cluster.args
    if not args.manifest or not args.namespace.startswith("qual-"):
        raise ValueError("prepare requires --manifest and a new qual-* namespace")
    obj = yaml.safe_load(args.manifest.read_text())
    if obj.get("kind") != "Tadoss":
        raise ValueError("Manifest must contain one Tadoss resource")
    # create, never apply: an existing namespace must never be adopted.
    ns = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": args.namespace, "labels": {DISPOSABLE: "true"}},
    }
    cluster.kubectl("create", "-f", "-", body=json.dumps(ns))
    obj["metadata"] = {
        "name": args.instance,
        "namespace": args.namespace,
        "labels": {DISPOSABLE: "true"},
    }
    obj.pop("status", None)
    obj["spec"].update(deletionProtection=False, deletionPolicy="Delete")
    cluster.kubectl("create", "-f", "-", body=json.dumps(obj))
    print(
        f"Created {args.namespace}/{args.instance}; "
        "configure OAuth and TLS before preflight"
    )


def prometheus(args, query, start=None, end=None):
    if not args.prometheus_url:
        raise ValueError(
            "--prometheus-url is required (a local port-forward is supported)"
        )
    params = {
        "query": query.replace("$namespace", args.namespace).replace(
            "$instance", args.instance
        )
    }
    endpoint = "/api/v1/query"
    if start is not None:
        endpoint += "_range"
        params.update(start=start, end=end, step=15)
    response = httpx.get(
        args.prometheus_url.rstrip("/") + endpoint, params=params, timeout=30
    )
    response.raise_for_status()
    result = response.json()
    if result["status"] != "success":
        raise ValueError("Prometheus query failed")
    return result["data"]["result"]


def preflight(cluster):
    args, cr = cluster.args, cluster.guard()
    if cr.get("status", {}).get("observedGeneration") != cr["metadata"]["generation"]:
        raise ValueError("Instance has not observed its current generation")
    conditions = {c["type"]: c for c in cr.get("status", {}).get("conditions", [])}
    for name in ["Ready", "PoolerReady", "MonitoringReady", "WorkerReady"]:
        if conditions.get(name, {}).get("status") != "True":
            raise ValueError(f"{name} is not ready: {conditions.get(name)}")
    if (
        cr["spec"].get("api", {}).get("autoscaling", {}).get("enabled")
        and conditions.get("AutoscalingReady", {}).get("status") != "True"
    ):
        raise ValueError("HPA metrics are unavailable")
    db = cluster.get("cluster", args.instance + "-db")
    pods = cluster.get("pods")["items"]
    for selector, value, expected in [
        ("cnpg.io/cluster", args.instance + "-db", 3),
        ("tadoss.livewyer.io/pooler", args.instance + "-pooler", 2),
    ]:
        nodes = {
            p["spec"].get("nodeName")
            for p in pods
            if p["metadata"].get("labels", {}).get(selector) == value
            and not p["metadata"].get("deletionTimestamp")
            and (
                selector != "cnpg.io/cluster"
                or p["metadata"]["labels"].get("cnpg.io/podRole") == "instance"
            )
        }
        if None in nodes or len(nodes) < expected:
            raise ValueError(f"{selector} needs {expected} separate Kubernetes nodes")
    sync = db["spec"].get("postgresql", {}).get("synchronous", {})
    if sync.get("dataDurability") != "required" or not sync.get("failoverQuorum"):
        raise ValueError(
            "Qualification requires synchronous replication with "
            "required durability and failover quorum"
        )
    targets = prometheus(args, 'up{namespace="$namespace",tadoss_instance="$instance"}')
    for suffix in ["api", "worker", "database", "pooler"]:
        selected = [
            r
            for r in targets
            if r["metric"].get("job", "").endswith("/" + args.instance + "-" + suffix)
        ]
        if not selected or any(r["value"][1] != "1" for r in selected):
            raise ValueError(
                f"Prometheus has no healthy {suffix} targets; check selectors, "
                "namespace discovery, credentials and network policy"
            )
    api = API(cluster, cr)
    api.request("GET", "/service").raise_for_status()
    cluster.snapshot(args.output / "preflight.json")
    save(args.output / "database-before.json", cluster.database())
    print(
        "Verified disposable target, HTTPS/OAuth, workers, synchronous CNPG, "
        "Pooler and Prometheus discovery"
    )
    return api


def seed_events(api, fixture):
    size, count = fixture["payload_bytes"], fixture["events_per_track"]
    chunk = min(1000, max(1, 900_000 // (size + 150)))
    for track in [*fixture["tracks"], fixture["restricted"]]:
        for offset in range(0, count, chunk):
            batch = [
                event(track, i, size) for i in range(offset, min(count, offset + chunk))
            ]
            response = api.request("POST", f"/tracks/{track}/events", json=batch)
            response.raise_for_status()
            if not response.json()["all_successful"]:
                raise ValueError("Seeding rejected events")


def drain_jobs(cluster):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        jobs = cluster.database()["jobs"] or []
        if not any(item["status"] in {"created", "started"} for item in jobs):
            return jobs
        time.sleep(3)
    raise TimeoutError("Background queue did not drain")


def reset_fixture(cluster, api, fixture):
    cluster.guard()
    expected = set(fixture["tracks"]) | {fixture["restricted"]}
    actual = set(
        json.loads(cluster.sql("SELECT coalesce(json_agg(id), '[]') FROM tracks"))
    )
    if expected != actual:
        raise ValueError(
            "Matrix reset requires exclusive ownership of every fixture track"
        )
    drain_jobs(cluster)
    # Only an exclusive, marked disposable database is reset between comparisons.
    cluster.sql("TRUNCATE TABLE events, jobs")
    seed_events(api, fixture)
    cluster.sql("ANALYZE events")
    drain_jobs(cluster)


def seed(cluster, api):
    args = cluster.args
    if args.fixture.exists():
        raise ValueError(
            "Fixture file already exists; choose a new path to avoid mixing datasets"
        )
    schema = str(uuid4())
    api.request(
        "PUT",
        "/schemas/" + schema,
        json={
            "id": schema,
            "schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    ).raise_for_status()
    tracks = [str(uuid4()) for _ in range(args.tracks + 1)]
    fixture = {
        "schema": schema,
        "tracks": tracks[:-1],
        "restricted": tracks[-1],
        "events_per_track": args.events,
        "payload_bytes": args.payload_bytes,
        "api_url": api.url,
        "namespace": args.namespace,
        "instance": args.instance,
        "tier": args.tier,
    }
    # One bounded batch at a time; memory use does not grow with dataset size.
    for track in tracks:
        api.request(
            "PUT",
            "/tracks/" + track,
            json={
                "id": track,
                "schema_id": schema,
                "timeline_reference": {
                    "entity_id": "urn:tadoss:qualification:" + track
                },
                "allow_overlaps": track != tracks[-1],
            },
        ).raise_for_status()
    seed_events(api, fixture)
    if args.webhook_url:
        synthetic = args.webhook_url == "fixture"
        if synthetic:
            url = f"https://qualification.{args.namespace}.svc/events"
            hook_token = base64.b64decode(
                cluster.get("secret", "qualification")["data"]["webhook_token"]
            ).decode()
        else:
            url = args.webhook_url
            hook_token = os.environ["TADOSS_QUALIFICATION_WEBHOOK_TOKEN"]
        if not url.startswith("https://"):
            raise ValueError("Webhook receiver must use HTTPS")
        result = api.request(
            "POST",
            "/service/webhooks",
            json={
                "url": url,
                "events": ["tracks/events_added", "tracks/events_updated"],
                "api_key_name": "X-Qualification-Token",
                "api_key_value": hook_token,
            },
        )
        result.raise_for_status()
        fixture["webhook"] = {
            "id": result.json()["id"],
            "url": url,
            "synthetic": synthetic,
        }
    save(args.fixture, redact(fixture))
    save(args.output / "database-seeded.json", cluster.database())
    print(f"Seeded {len(tracks) * args.events} events; fixture: {args.fixture}")


def check_fixture(cluster, api):
    fixture = json.loads(cluster.args.fixture.read_text())
    if (fixture["namespace"], fixture["instance"], fixture["api_url"]) != (
        cluster.args.namespace,
        cluster.args.instance,
        api.url,
    ):
        raise ValueError("Fixture belongs to another target")
    return fixture


def webhook_stats(cluster, api, fixture):
    if not fixture.get("webhook", {}).get("synthetic"):
        return None
    data = cluster.get("secret", "qualification")["data"]
    token = base64.b64decode(data["webhook_token"]).decode()
    url = base64.b64decode(data["token_endpoint"]).decode().removesuffix("/token")
    response = api.client.get(
        url + "/stats", headers={"Authorization": "Bearer " + token}
    )
    response.raise_for_status()
    return response.json()


def run(cluster, api, label):
    args = cluster.args
    fixture = check_fixture(cluster, api)
    if (
        args.batch > fixture["events_per_track"]
        or args.batch * (args.payload_bytes + 150) > 1_048_576
    ):
        raise ValueError(
            "Batch needs enough seeded events and must fit the 1 MiB request limit"
        )
    path = (args.output / label).resolve()
    path.mkdir(parents=True, exist_ok=False)
    cluster.snapshot(path / "before.json")
    save(path / "fixture.json", fixture)
    previous_jobs = drain_jobs(cluster) if fixture.get("webhook") else []
    previous_errors = sum(j["count"] for j in previous_jobs if j["status"] == "error")
    webhook_before = webhook_stats(cluster, api, fixture)
    credentials = api.credentials
    env = {
        **os.environ,
        "TADOSS_URL": api.url,
        "TADOSS_OAUTH_TOKEN_URL": credentials["token_endpoint"],
        "TADOSS_OAUTH_CLIENT_ID": credentials["client_id"],
        "TADOSS_OAUTH_CLIENT_SECRET": credentials["client_secret"],
        "TADOSS_OAUTH_SCOPES": credentials["scopes"],
        "FIXTURE": "/results/fixture.json",
        "SUMMARY": "/results/summary.json",
        "RUN_ID": str(uuid4()),
        "RATE": str(args.rate),
        "DURATION": args.duration,
        "BATCH": str(args.batch),
        "PAYLOAD_BYTES": str(args.payload_bytes),
        "WORKLOAD": args.workload,
        "VUS": str(args.vus),
        "MAX_VUS": str(args.max_vus),
    }
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--mount",
        f"type=bind,src={ASSETS},dst=/scripts,readonly",
        "--mount",
        f"type=bind,src={path},dst=/results",
    ]
    for key in {
        "FIXTURE",
        "SUMMARY",
        "RUN_ID",
        "RATE",
        "DURATION",
        "BATCH",
        "PAYLOAD_BYTES",
        "WORKLOAD",
        "VUS",
        "MAX_VUS",
        "TADOSS_URL",
        "TADOSS_OAUTH_TOKEN_URL",
        "TADOSS_OAUTH_CLIENT_ID",
        "TADOSS_OAUTH_CLIENT_SECRET",
        "TADOSS_OAUTH_SCOPES",
    }:
        command += ["--env", key]
    if args.ca_file:
        command += [
            "--mount",
            f"type=bind,src={Path(args.ca_file).resolve()},dst=/ca.pem,readonly",
            "--env",
            "SSL_CERT_FILE=/ca.pem",
        ]
    command += [K6_IMAGE, "run", "/scripts/workload.js"]
    stop = threading.Event()

    def sample():
        with (path / "database.jsonl").open("w") as output:
            while not stop.is_set():
                try:
                    data = cluster.database()
                except (subprocess.SubprocessError, KeyError, ValueError) as exc:
                    data = {"time": time.time(), "sample_error": type(exc).__name__}
                output.write(json.dumps(data) + "\n")
                output.flush()
                stop.wait(15)

    thread = threading.Thread(target=sample, daemon=True)
    started = time.time()
    thread.start()
    try:
        with (path / "k6.log").open("w") as log:
            result = subprocess.run(
                command, env=env, stdout=log, stderr=subprocess.STDOUT, check=False
            )
    finally:
        stop.set()
        thread.join(timeout=35)
    ended = time.time()
    cluster.snapshot(path / "after.json")
    queries = json.loads((ASSETS / "queries.json").read_text())
    series = {
        name: prometheus(args, query, started, ended) for name, query in queries.items()
    }
    save(path / "prometheus.json", series)
    delivery_failed = False
    if webhook_before is not None:
        deadline = time.monotonic() + 120
        pending = [{}]
        while time.monotonic() < deadline:
            jobs = cluster.database()["jobs"] or []
            pending = [
                item for item in jobs if item["status"] in {"created", "started"}
            ]
            if not pending:
                break
            time.sleep(3)
        after = webhook_stats(cluster, api, fixture)
        summary_path = path / "summary.json"
        summary = (
            json.loads(summary_path.read_text())
            if summary_path.exists()
            else {"metrics": {}}
        )
        committed = (
            summary["metrics"]
            .get("events_committed", {})
            .get("values", {})
            .get("count", 0)
        )
        delivery_failed = (
            bool(pending)
            or after["events"] - webhook_before["events"] < committed
            or sum(j["count"] for j in jobs if j["status"] == "error") > previous_errors
        )
        save(
            path / "webhooks.json",
            {
                "before": webhook_before,
                "after": after,
                "pending": pending,
                "committed_events": committed,
                "failed": delivery_failed,
            },
        )
    image = json.loads(
        subprocess.check_output(["docker", "image", "inspect", K6_IMAGE], text=True)
    )[0]
    save(
        path / "run.json",
        {
            "start": started,
            "end": ended,
            "exit_code": result.returncode,
            "tier": args.tier,
            "rate": args.rate,
            "duration": args.duration,
            "batch": args.batch,
            "payload_bytes": args.payload_bytes,
            "workload": args.workload,
            "telemetry_missing": [
                name for name, values in series.items() if not values
            ],
            "delivery_failed": delivery_failed,
            "k6_digest": image.get("RepoDigests"),
            "qualification": "functional-only"
            if args.tier == "kind"
            else "measurement; requires review against deployment SLOs",
        },
    )
    print(f"{label}: k6 exit {result.returncode}; reports: {path}")
    return result.returncode or int(delivery_failed)


def matrix(cluster, api):
    args, cr = cluster.args, cluster.guard()
    original = copy.deepcopy(cr["spec"].get("api", {}))
    fixture = check_fixture(cluster, api)
    failed = False
    try:
        for replicas in [1, 2, 4, 8]:
            cluster.patch_instance(
                {"api": {"replicaCount": replicas, "autoscaling": {"enabled": False}}}
            )
            for repeat in range(1, 4):
                reset_fixture(cluster, api, fixture)
                failed |= bool(run(cluster, api, f"fixed-{replicas}-repeat-{repeat}"))
        cluster.patch_instance(
            {
                "api": {
                    "replicaCount": None,
                    "autoscaling": {
                        "enabled": True,
                        "minReplicas": 2,
                        "maxReplicas": 8,
                    },
                }
            }
        )
        for repeat in range(1, 4):
            reset_fixture(cluster, api, fixture)
            failed |= bool(run(cluster, api, f"hpa-repeat-{repeat}"))
        reset_fixture(cluster, api, fixture)
        args.duration = args.soak_duration
        failed |= bool(run(cluster, api, "hpa-soak"))
    finally:
        cluster.patch_instance(
            {
                "api": {
                    "replicaCount": original.get("replicaCount"),
                    "autoscaling": original.get("autoscaling"),
                }
            }
        )
    return int(failed)


def fault(cluster, api):
    args = cluster.args
    cluster.guard()
    fixture = check_fixture(cluster, api)
    track, run_id = fixture["tracks"][0], str(uuid4())
    ledger = args.output / ("fault-" + args.fault + ".jsonl")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    if ledger.exists():
        raise ValueError("Fault ledger exists; choose a new output directory")
    completed, cancel, trigger_error = threading.Event(), threading.Event(), []
    before = cluster.get("cluster", args.instance + "-db")
    primary = before["status"]["currentPrimary"]
    pods = [
        p["metadata"]["name"]
        for p in cluster.get("pods")["items"]
        if p["metadata"].get("labels", {}).get("cnpg.io/cluster")
        == args.instance + "-db"
        and p["metadata"].get("labels", {}).get("cnpg.io/podRole") == "instance"
    ]
    standbys = [p for p in pods if p != primary]

    def trigger():
        try:
            if cancel.wait(5):
                return
            if args.fault == "switchover":
                current = cluster.get("cluster", args.instance + "-db")
                if current["status"]["currentPrimary"] != primary:
                    raise ValueError("Primary changed before switchover")
                # Same status transition as the CNPG promote plugin.
                cluster.kubectl(
                    "patch",
                    "cluster",
                    args.instance + "-db",
                    "--subresource=status",
                    "--type=merge",
                    "-p",
                    json.dumps(
                        {
                            "metadata": {
                                "resourceVersion": current["metadata"][
                                    "resourceVersion"
                                ]
                            },
                            "status": {
                                "targetPrimary": standbys[0],
                                "targetPrimaryTimestamp": time.strftime(
                                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                                ),
                            },
                        }
                    ),
                )
            elif args.fault == "standby-loss":
                cluster.kubectl(
                    "annotate",
                    "cluster",
                    args.instance + "-db",
                    "cnpg.io/fencedInstances=" + json.dumps(standbys),
                    "--overwrite",
                )
                cancel.wait(20)
            else:
                selector = {
                    "api-delete": (
                        f"app.kubernetes.io/instance={args.instance},"
                        "app.kubernetes.io/component=api"
                    ),
                    "pooler-delete": (
                        f"tadoss.livewyer.io/pooler={args.instance}-pooler"
                    ),
                }.get(args.fault)
                pod = (
                    primary
                    if not selector
                    else json.loads(
                        cluster.kubectl("get", "pods", "-l", selector, "-o", "json")
                    )["items"][0]["metadata"]["name"]
                )
                cluster.kubectl(
                    "delete", "pod", pod, "--grace-period=0", "--force", "--wait=false"
                )
        except (subprocess.SubprocessError, IndexError, KeyError, ValueError) as exc:
            trigger_error.append(type(exc).__name__)
        finally:
            if args.fault == "standby-loss":
                try:
                    cluster.kubectl(
                        "annotate",
                        "cluster",
                        args.instance + "-db",
                        "cnpg.io/fencedInstances-",
                    )
                except subprocess.SubprocessError as exc:
                    trigger_error.append("unfence: " + type(exc).__name__)
            completed.set()

    thread = threading.Thread(target=trigger, daemon=True)
    thread.start()
    started, rows = time.monotonic(), []
    try:
        with ledger.open("w") as output:
            while time.monotonic() - started < 90 or not completed.is_set():
                e = event(run_id, len(rows), 64)
                row = {"event": e, "track": track, "at": time.time()}
                try:
                    response = api.request("POST", f"/tracks/{track}/events", json=e)
                    row["status"] = response.status_code
                    row["acknowledged"] = (
                        response.status_code == 200
                        and response.json().get("all_successful") is True
                    )
                except (httpx.HTTPError, ValueError):
                    row["acknowledged"] = False
                row["completed_at"] = time.time()
                row["duration_seconds"] = row["completed_at"] - row["at"]
                rows.append(row)
                output.write(json.dumps(row) + "\n")
                output.flush()
                time.sleep(0.1)
    finally:
        cancel.set()
        thread.join(timeout=70)
    # A connection failure is ambiguous. Read it back; never replay the write.
    missing, ambiguous, ambiguous_present = [], [], []
    for row in rows:
        response = api.request(
            "GET",
            f"/tracks/{track}/events",
            params={"timerange": row["event"]["timerange"], "limit": 1000},
        )
        response.raise_for_status()
        present = row["event"] in response.json()
        if row["acknowledged"] and not present:
            missing.append(row["event"]["id"])
        if not row["acknowledged"]:
            ambiguous.append(row["event"]["id"])
            if present:
                ambiguous_present.append(row["event"]["id"])
    acknowledgements = [r["completed_at"] for r in rows if r["acknowledged"]]
    report = {
        "scenario": args.fault,
        "longest_acknowledgement_gap_seconds": max(
            (b - a for a, b in pairwise(acknowledgements)),
            default=0,
        ),
        "max_request_seconds": max(r["duration_seconds"] for r in rows),
        "acknowledged": sum(bool(r["acknowledged"]) for r in rows),
        "missing_acknowledged": missing,
        "ambiguous": ambiguous,
        "ambiguous_present": ambiguous_present,
        "trigger_errors": trigger_error,
        "elapsed_seconds": time.monotonic() - started,
    }
    save(args.output / ("fault-" + args.fault + ".json"), report)
    cluster.snapshot(args.output / ("fault-" + args.fault + "-after.json"))
    return int(bool(missing or trigger_error) or not report["acknowledged"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=["prepare", "fixture", "preflight", "seed", "run", "matrix", "fault"],
    )
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--instance", default="tadoss")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--fixture-url")
    parser.add_argument("--fixture-issuer", default="tadoss-selfsigned")
    parser.add_argument("--api-url")
    parser.add_argument("--ca-file")
    parser.add_argument("--prometheus-url")
    parser.add_argument("--tier", choices=["kind", "dedicated"], default="kind")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "reports/qualification" / time.strftime("%Y%m%d-%H%M%S"),
    )
    parser.add_argument(
        "--fixture", type=Path, default=ROOT / ".local/qualification-fixture.json"
    )
    parser.add_argument("--tracks", type=int, default=16)
    parser.add_argument("--events", type=int, default=1000)
    parser.add_argument("--payload-bytes", type=int, default=64)
    parser.add_argument("--webhook-url")
    parser.add_argument("--batch", type=int, choices=[1, 100, 1000], default=100)
    parser.add_argument("--rate", type=int, default=20)
    parser.add_argument("--duration", default="5m")
    parser.add_argument("--soak-duration", default="30m")
    parser.add_argument("--vus", type=int, default=32)
    parser.add_argument("--max-vus", type=int, default=512)
    parser.add_argument(
        "--workload",
        choices=[
            "read-page",
            "read-range",
            "insert",
            "update",
            "mixed",
            "hot",
            "no-overlap",
        ],
        default="mixed",
    )
    parser.add_argument(
        "--fault",
        choices=[
            "api-delete",
            "pooler-delete",
            "primary-delete",
            "switchover",
            "standby-loss",
        ],
        default="api-delete",
    )
    args = parser.parse_args()
    if (
        min(
            args.tracks,
            args.events,
            args.payload_bytes,
            args.rate,
            args.vus,
            args.max_vus,
        )
        < 1
        or args.max_vus < args.vus
    ):
        parser.error("Counts and rates must be positive; max-vus must cover vus")
    if any(
        not re.fullmatch(r"[1-9][0-9]*(s|m|h)", d)
        for d in (args.duration, args.soak_duration)
    ):
        parser.error("Duration must be a positive number followed by s, m or h")
    cluster = Cluster(args)
    if args.command == "prepare":
        prepare(cluster)
        return 0
    if args.command == "fixture":
        from qualification.fixtures import install

        install(cluster)
        return 0
    api = preflight(cluster)
    try:
        if args.command == "seed":
            seed(cluster, api)
        if args.command == "run":
            return run(cluster, api, "run")
        if args.command == "matrix":
            return matrix(cluster, api)
        if args.command == "fault":
            return fault(cluster, api)
        return 0
    finally:
        api.client.close()


if __name__ == "__main__":
    raise SystemExit(main())
