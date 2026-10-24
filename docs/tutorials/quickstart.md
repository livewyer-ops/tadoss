# Local development and first installation

Clone the repository with submodules, install the tools in `aqua.yml`, then run `task setup` and `task dev:db`. The local PostgreSQL port is 15432. Tests create isolated schemas and leave other schemas intact.

```sh
TADOSS_DEV=true uv run --project src tadoss migrate
TADOSS_DEV=true uv run --project src tadoss serve
```

Open `http://127.0.0.1:8000/docs`. Start the worker in another terminal with `TADOSS_DEV=true uv run --project src tadoss worker` to process notifications and deferred deletions.

For Kubernetes, use a disposable cluster:

```sh
task kind:test CLUSTER=tadoss-demo PROFILE=local-kind HTTP_PORT=18080 HTTPS_PORT=18443
task env:status ENV_DIR=.local/environments/tadoss-demo-local-kind KUBECONFIG=.local/tadoss-demo.kubeconfig
```

Choose unused host ports and a unique cluster name. Kind tests use self-signed TLS. Production environments need a trusted issuer or an existing TLS Secret. See [deployment](../../deploy/README.md) for environment creation and prerequisite ownership.
