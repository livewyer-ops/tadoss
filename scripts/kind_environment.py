import argparse
from pathlib import Path

import yaml
from environment import image_override, init_environment, write_yaml
from tadoss import __version__

parser = argparse.ArgumentParser()
parser.add_argument("--directory", type=Path, required=True)
parser.add_argument("--profile", required=True)
parser.add_argument("--tag", required=True)
parser.add_argument("--runtime-image", default="")
parser.add_argument("--operator-image", default="")
args = parser.parse_args()
directory = args.directory.resolve()
operator_image = args.operator_image or "livewyer/tadoss-operator:" + args.tag
override = image_override(operator_image)
runtime_image = args.runtime_image or "livewyer/tadoss-api:" + args.tag
image_override(runtime_image)
if not directory.exists():
    init_environment(directory, args.profile, "tadoss.localtest.me", __version__)
write_yaml(
    directory / "platform-values.yaml",
    {
        "tls": {"mode": "selfSigned"},
        "platform": {
            "traefik": {
                "values": {
                    "service": {"type": "NodePort"},
                    "ports": {
                        "web": {"nodePort": 30080},
                        "websecure": {"nodePort": 30443},
                    },
                }
            }
        },
    },
)
defaults = yaml.safe_load((directory / "operator/defaults.yaml").read_text())
defaults["clusterIssuer"] = "tadoss-selfsigned"
write_yaml(directory / "operator/defaults.yaml", defaults)
overlay = yaml.safe_load((directory / "operator/kustomization.yaml").read_text())
overlay["images"] = [{"name": "livewyer/tadoss-operator", **override}]
write_yaml(directory / "operator/kustomization.yaml", overlay)
resource = yaml.safe_load((directory / "instances/tadoss/tadoss.yaml").read_text())
if "@sha256:" in args.runtime_image:
    resource["spec"].pop("api", None)
else:
    resource["spec"]["api"] = {
        "image": args.runtime_image or "livewyer/tadoss-api:" + args.tag
    }
write_yaml(directory / "instances/tadoss/tadoss.yaml", resource)
