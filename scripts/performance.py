# ruff: noqa: S607
"""Repeatable real-HTTP benchmarks against an isolated local TADOSS instance."""

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from uuid import uuid4

import httpx
import jwt
import psycopg
from cryptography.hazmat.primitives.asymmetric import rsa
from psycopg import sql
from psycopg.conninfo import make_conninfo
from tadoss.adapters import postgres as store

ROOT = Path(__file__).resolve().parents[1]
CASES = ("page", "range", "write-1", "write-100", "write-1000", "hot-100", "mixed")


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] if ordered else None


def successful_run(results, allow_overload=False):
    allowed = {"200", "503"} if allow_overload else {"200"}
    return bool(results) and all(
        result["requests"] > 0
        and set(result["statuses"]) <= allowed
        and not result["rejected_events"]
        for result in results
    )


def process_stats(pid):
    """Linux process CPU and RSS; unavailable platforms report nulls."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()
        return {
            "cpu_seconds": (int(fields[11]) + int(fields[12]))
            / os.sysconf("SC_CLK_TCK"),
            "rss_bytes": int(fields[21]) * os.sysconf("SC_PAGE_SIZE"),
        }
    except (OSError, ValueError):
        return {"cpu_seconds": None, "rss_bytes": None}


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def isolated_database():
    url = os.getenv("TADOSS_TEST_DATABASE", store.DEFAULT_DATABASE)
    namespace = "perf_" + uuid4().hex
    with psycopg.connect(url) as connection:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace))
        )
    try:
        yield make_conninfo(url, options=f"-c search_path={namespace}")
    finally:
        with psycopg.connect(url) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(namespace))
            )


@contextmanager
def receiver():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="benchmark", use="sig", alg="RS256")
    jwks = json.dumps({"keys": [jwk]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(jwks)))
            self.end_headers()
            self.wfile.write(jwks)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    issuer = f"http://127.0.0.1:{server.server_port}"
    token = jwt.encode(
        {
            "sub": "benchmark",
            "iss": issuer,
            "aud": "benchmark",
            "exp": time.time() + 86400,
            "scope": "tads-api/read tads-api/write tads-api/delete",
        },
        key,
        algorithm="RS256",
        headers={"kid": "benchmark"},
    )
    try:
        yield issuer, token
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def spawn(stack, command, env, log):
    output = stack.enter_context(log.open("w"))
    log.chmod(0o600)
    process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=output, stderr=output)  # noqa: S603
    stack.callback(stop_process, process)
    return process


def wait_ready(client, process):
    for _ in range(200):
        if process.poll() is not None:
            raise RuntimeError("Benchmark process stopped; inspect its private log")
        try:
            if client.get("/readyz").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise RuntimeError("Benchmark readiness timed out")


def check(response, status=200):
    if response.status_code != status:
        raise RuntimeError(
            f"Fixture request returned {response.status_code}, expected {status}"
        )
    return response.json() if response.content else None


def seed(client, tracks, events, payload_bytes):
    schema = str(uuid4())
    check(
        client.put(
            f"/schemas/{schema}",
            json={
                "id": schema,
                "schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
        ),
        201,
    )
    fixtures = []
    for _ in range(tracks):
        track = str(uuid4())
        check(
            client.put(
                f"/tracks/{track}",
                json={
                    "id": track,
                    "schema_id": schema,
                    "editable_events": True,
                    "timeline_reference": {"entity_id": "urn:tadoss:performance"},
                },
            ),
            201,
        )
        batch = [
            {
                "id": str(uuid4()),
                "timerange": f"[{index}:0_{index + 1}:0)",
                "payload": {"text": "x" * payload_bytes},
            }
            for index in range(events)
        ]
        path = f"/tracks/{track}/events"
        for offset in range(0, events, 1000):
            result = check(client.post(path, json=batch[offset : offset + 1000]))
            if not result["all_successful"]:
                raise RuntimeError("Fixture events were rejected")
        fixtures.append((path, batch))
    return fixtures


async def exercise(client, fixtures, case, concurrency, duration, pid):
    latencies = []
    statuses = Counter()
    transport_errors = []
    rejected_events = 0
    peak_rss = 0
    before = process_stats(pid)
    started = time.perf_counter()
    deadline = started + duration
    done = asyncio.Event()

    async def sample():
        nonlocal peak_rss
        while not done.is_set():
            peak_rss = max(peak_rss, process_stats(pid)["rss_bytes"] or 0)
            await asyncio.sleep(0.1)

    async def user(index):
        nonlocal rejected_events
        path, batch = fixtures[0 if case.startswith("hot") else index % len(fixtures)]
        size = int(case.rsplit("-", 1)[1]) if "-" in case else 1
        body = json.dumps(batch[:size]).encode()
        cursor = None
        request_number = 0
        while time.perf_counter() < deadline:
            request_started = time.perf_counter()
            try:
                if "-" in case or (case == "mixed" and request_number % 4 == 0):
                    response = await client.post(
                        path, content=body, headers={"Content-Type": "application/json"}
                    )
                    if response.status_code == 200:
                        rejected_events += sum(
                            row["http_status_code"] >= 300
                            for row in response.json()["responses"]
                        )
                else:
                    params = {"limit": "100"}
                    if case == "range":
                        params["timerange"] = "[10:0_30:0)"
                    elif cursor:
                        params["page"] = cursor
                    response = await client.get(path, params=params)
                    cursor = response.headers.get("X-Paging-NextKey")
                statuses[str(response.status_code)] += 1
            except httpx.TransportError as exc:
                statuses[type(exc).__name__] += 1
                if len(transport_errors) < 5:
                    transport_errors.append(
                        {"type": type(exc).__name__, "message": str(exc)[:256]}
                    )
            latencies.append((time.perf_counter() - request_started) * 1000)
            request_number += 1

    sampler = asyncio.create_task(sample())
    try:
        await asyncio.gather(*(user(index) for index in range(concurrency)))
    finally:
        done.set()
        await sampler
    elapsed = time.perf_counter() - started
    after = process_stats(pid)
    return {
        "case": case,
        "concurrency": concurrency,
        "elapsed_seconds": elapsed,
        "requests": len(latencies),
        "requests_per_second": len(latencies) / elapsed,
        "http_200_per_second": statuses["200"] / elapsed,
        "latency_ms": {
            name: percentile(latencies, fraction)
            for name, fraction in [("p50", 0.5), ("p95", 0.95), ("p99", 0.99)]
        },
        "statuses": dict(statuses),
        "transport_error_samples": transport_errors,
        "rejected_events": rejected_events,
        "api_peak_rss_bytes": peak_rss or None,
        "api_start_rss_bytes": before["rss_bytes"],
        "api_end_rss_bytes": after["rss_bytes"],
        "api_cpu_seconds": None
        if before["cpu_seconds"] is None
        else after["cpu_seconds"] - before["cpu_seconds"],
    }


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "reports/performance/report.json"
    )
    parser.add_argument("--duration", type=float, default=5)
    parser.add_argument("--warmup", type=float, default=1)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 10, 50, 100])
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument("--tracks", type=int, default=8)
    parser.add_argument("--events", type=int, default=1000)
    parser.add_argument("--pool-size", type=int, default=10)
    parser.add_argument("--payload-bytes", type=int, default=256)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument(
        "--allow-overload",
        action="store_true",
        help="Accept HTTP 503 during saturation; other failures still fail",
    )
    parser.add_argument("--loop", choices=["asyncio", "uvloop"], default="asyncio")
    parser.add_argument("--http", choices=["h11", "httptools"], default="h11")
    args = parser.parse_args()
    if any(
        not math.isfinite(value) or value <= 0
        for value in [
            args.duration,
            args.warmup,
            args.repeats,
            args.tracks,
            args.events,
            args.pool_size,
            args.payload_bytes,
            *args.concurrency,
        ]
    ):
        parser.error("durations, sizes and concurrency must be positive and finite")
    for case in args.cases:
        if "-" in case and args.events < int(case.rsplit("-", 1)[1]):
            parser.error(f"{case} requires enough fixture events for its batch size")
    return args


def main():
    def terminate(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    args = arguments()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    packages = {}
    for name in (
        "tadoss",
        "fastapi",
        "starlette",
        "uvicorn",
        "psycopg",
        "uvloop",
        "httptools",
    ):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src/app/tadoss").rglob("*")):
        if path.suffix in {".py", ".json"}:
            digest.update(str(path.relative_to(ROOT)).encode())
            digest.update(path.read_bytes())
    report = {
        "format": 1,
        "success": False,
        "runtime_sha256": digest.hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)
        ),
        "machine": {
            "architecture": platform.machine(),
            "platform": platform.platform(),
            "cpus": os.cpu_count(),
            "python": platform.python_version(),
        },
        "packages": packages,
        "configuration": {
            key: value for key, value in vars(args).items() if key != "output"
        },
        "results": [],
    }
    try:
        with (
            isolated_database() as database,
            receiver() as (issuer, token),
            ExitStack() as stack,
        ):
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("TADOSS_", "OTEL_"))
            }
            env.update(
                TADOSS_DATABASE=database,
                TADOSS_DEV="true",
                TADOSS_HOST="127.0.0.1",
                TADOSS_PORT=str(free_port()),
                TADOSS_POOL_MAX_SIZE=str(args.pool_size),
                TADOSS_OAUTH2_ISSUER=issuer,
                TADOSS_OAUTH2_JWKS_URI=issuer + "/jwks",
                TADOSS_OAUTH2_AUDIENCE="benchmark",
                TADOSS_LOG_LEVEL="warning",
                TADOSS_WORKER_POLL_INTERVAL_SECONDS="0.05",
                TADOSS_WORKER_HEALTH_PORT=str(free_port()),
                TADOSS_WEBHOOK_ALLOWED_HOSTS="127.0.0.1",
            )
            with store.create_pool(database) as pool:
                store.initialize(pool)
            command = [
                sys.executable,
                "-c",
                "import uvicorn; from tadoss.telemetry import configure_logging; "
                "configure_logging('warning'); "
                "uvicorn.run('tadoss.api:create_app', factory=True, host='127.0.0.1', "
                f"port={env['TADOSS_PORT']}, loop={args.loop!r}, http={args.http!r}, "
                "workers=1, access_log=False, proxy_headers=False, log_config=None)",
            ]
            api = spawn(stack, command, env, args.output.with_suffix(".api.log"))
            base_url = f"http://127.0.0.1:{env['TADOSS_PORT']}"
            client = stack.enter_context(
                httpx.Client(
                    base_url=base_url,
                    headers={"Authorization": "Bearer " + token},
                    timeout=60,
                )
            )
            wait_ready(client, api)
            cold_started = time.perf_counter()
            check(client.get("/service"))
            report["cold_oauth_ms"] = (time.perf_counter() - cold_started) * 1000
            fixtures = seed(client, args.tracks, args.events, args.payload_bytes)
            if args.worker:
                check(
                    client.post(
                        "/service/webhooks",
                        json={
                            "url": issuer + "/events",
                            "events": ["tracks/events_added", "tracks/events_updated"],
                        },
                    ),
                    201,
                )
                worker = spawn(
                    stack,
                    [sys.executable, "-m", "tadoss", "worker"],
                    env,
                    args.output.with_suffix(".worker.log"),
                )
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{env['TADOSS_WORKER_HEALTH_PORT']}"
                ) as health:
                    wait_ready(health, worker)

            async def run():
                async with httpx.AsyncClient(
                    base_url=base_url,
                    headers=dict(client.headers),
                    timeout=60,
                    limits=httpx.Limits(
                        max_connections=max(args.concurrency),
                        max_keepalive_connections=max(args.concurrency),
                        # Retire idle sockets before Uvicorn's 5s keepalive deadline.
                        keepalive_expiry=2,
                    ),
                ) as load:
                    for case in args.cases:
                        for concurrency in args.concurrency:
                            await exercise(
                                load, fixtures, case, concurrency, args.warmup, api.pid
                            )
                            for repeat in range(args.repeats):
                                result = await exercise(
                                    load,
                                    fixtures,
                                    case,
                                    concurrency,
                                    args.duration,
                                    api.pid,
                                )
                                result["repeat"] = repeat + 1
                                report["results"].append(result)
                                args.output.write_text(
                                    json.dumps(report, indent=2) + "\n"
                                )
                                print(
                                    f"{case} clients={concurrency} run={repeat + 1}: "
                                    f"{result['requests_per_second']:.1f} req/s, "
                                    f"p95={result['latency_ms']['p95']:.1f}ms",
                                    flush=True,
                                )

            asyncio.run(run())
            args.output.with_suffix(".metrics.txt").write_text(
                client.get("/metrics").raise_for_status().text
            )
            with psycopg.connect(database) as connection:
                report["remaining_jobs"] = dict(
                    connection.execute(
                        "SELECT status,count(*) FROM jobs GROUP BY status"
                    ).fetchall()
                )
                report["postgres_version"] = connection.execute(
                    "SHOW server_version"
                ).fetchone()[0]
            report["overload_responses"] = sum(
                result["statuses"].get("503", 0) for result in report["results"]
            )
            report["success"] = successful_run(report["results"], args.allow_overload)
    finally:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    if not report["success"]:
        raise SystemExit("Benchmark recorded failed requests; inspect the report")


if __name__ == "__main__":
    main()
