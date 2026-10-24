# TADOSS

TADOSS implements the BBC Time Addressable Data Store API with PostgreSQL and a Kubernetes operator. It stores schemas, tracks and time-addressed events. A worker delivers webhooks and completes bounded asynchronous deletions.

This is a pre-release project. The current contract is TADS 0.1, pinned at `d2b90e52d54db947f4c8ddab571538ed18ab3052`. Runtime, schema and operator compatibility are recorded in [release metadata](src/app/tadoss/release.json).

## Start

```sh
git submodule update --init --recursive
aqua install
task setup
task dev:db
TADOSS_DEV=true uv run --project src tadoss migrate
TADOSS_DEV=true uv run --project src tadoss serve
```

Development mode permits anonymous access. Deployments require authentication. For a disposable Kubernetes installation:

```sh
task kind:test CLUSTER=tadoss-demo PROFILE=local-kind
```

Use a unique cluster name. `task kind:down CLUSTER=tadoss-demo` deletes that cluster and its data.

## Project

| Group | Contents |
| --- | --- |
| [src](src/README.md) | Python API, worker, migrations, pinned upstream specification |
| [operator](operator/README.md) | Go controller, CRD, release catalogue, envtest and Chainsaw tests |
| [deploy](deploy/README.md) | Platform Helmfile, operator Kustomize base, environment and instance overlays |
| [.tasks](.tasks) | Development, environment, instance, test, security and release commands |
| [docs](docs/README.md) | Tutorials, operational guides, reference and design explanations |

See [contributing](CONTRIBUTING.md), [security reporting](SECURITY.md), [support](SUPPORT.md) and the [changelog](CHANGELOG.md).
