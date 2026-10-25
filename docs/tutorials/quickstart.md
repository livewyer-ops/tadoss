# Local development and first installation

Clone the repository with submodules, install the tools in `aqua.yml`, then run `task setup` and `task dev:db`. The local PostgreSQL port is 15432. Tests create isolated schemas and leave other schemas intact.

```sh
TADOSS_DEV=true uv run --project src tadoss migrate
TADOSS_DEV=true uv run --project src tadoss serve
```

Open `http://127.0.0.1:8000/docs`. Start the worker in another terminal with `TADOSS_DEV=true uv run --project src tadoss worker` to process notifications and deferred deletions.

## Recreate a disposable development database

Compose preserves its database volume between restarts. If the [changelog](../../CHANGELOG.md) requires a fresh database, the following commands delete all data in this repository's Compose database and initialise it again:

```sh
docker compose --profile app down --volumes
task dev:db
TADOSS_DEV=true uv run --project src tadoss migrate
```

For data you need to keep, preserve the previous build and its database backup first. Export the required records with that build and import them into a separate, freshly initialised database. For external or retained databases, use the database owner's backup and recreation process; changing the migration ledger does not convert the schema. See [backup and recovery](../how-to/backup-recovery.md).

## Kubernetes

For Kubernetes, use a disposable cluster:

```sh
task kind:test CLUSTER=tadoss-demo PROFILE=local-kind HTTP_PORT=18080 HTTPS_PORT=18443
task env:status ENV_DIR=.local/environments/tadoss-demo-local-kind KUBECONFIG=.local/tadoss-demo.kubeconfig
```

Choose unused host ports and a unique cluster name. Kind tests use self-signed TLS. Production environments need a trusted issuer or an existing TLS Secret. See [deployment](../../deploy/README.md) for environment creation and prerequisite ownership.
