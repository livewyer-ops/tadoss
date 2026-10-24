"""Render a Kind configuration with explicit local port bindings."""

import argparse
from pathlib import Path

import yaml

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--profile", required=True)
parser.add_argument("--http-port", type=int, default=18080)
parser.add_argument("--https-port", type=int, default=18443)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
for port in (args.http_port, args.https_port):
    if not 1024 <= port <= 65535:
        parser.error("Host ports must be between 1024 and 65535")
source = "multi-node" if args.profile == "multi-server" else "single-node"
config = yaml.safe_load(Path(f"deploy/kind/{source}.yaml").read_text())
for mapping, port in zip(
    config["nodes"][0]["extraPortMappings"],
    (args.http_port, args.https_port),
    strict=True,
):
    mapping["hostPort"] = port
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(yaml.safe_dump(config, sort_keys=False))
