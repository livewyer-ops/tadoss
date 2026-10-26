# Local development and first installation

Clone the repository with submodules, install the tools in `aqua.yml`, then run `task setup` and `task dev:db`. The local PostgreSQL port is 15432. Tests create isolated schemas and leave other schemas intact.

```sh
TADOSS_DEV=true uv run --project src tadoss migrate
TADOSS_DEV=true uv run --project src tadoss serve
```

Open `http://127.0.0.1:8000/docs`. Start the worker in another terminal with `TADOSS_DEV=true uv run --project src tadoss worker` to process notifications and deferred deletions.

## Create a transcript

With the API running, execute the [transcript example](../../examples/transcript.py):

```sh
uv run --project src python examples/transcript.py
```

It creates a payload schema and a track, writes transcript events, then prints events overlapping the first two seconds. It leaves the records available for inspection. Set `TADOSS_URL` and `TADOSS_API_TOKEN` when using an authenticated installation.

The track's `timeline_reference.entity_id` identifies the media timeline; it does not import media or contact TAMOSS. Set `TAMS_ENTITY` to the `tams://<host>/sources/<source-id>` reference for your TAMOSS Source before running the example. TADOSS event ranges must use that Source's timeline. See the pinned BBC [TADS and TAMS application note](../../src/vendor/bbc-tads/docs/appnotes/0001-using-tads-with-tams.md).

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

Load the cluster's kubeconfig, API URL and authentication into your current shell:

```sh
credentials=$(task kind:credentials CLUSTER=tadoss-demo) && eval "$credentials"
unset credentials
kubectl get pods -n tads
```

This development setup exports `KUBECONFIG`, `TADOSS_URL`,
`TADOSS_AUTH_METHOD=query` and `TADOSS_API_TOKEN`. The token uses the
`access_token` query parameter. Supply `API_URL=https://<ingress-host>:18443`
for access through this Kind host port and trust the cluster certificate.
Retrieve credentials again after switching instances or rotating them.
Shared deployments can use [OAuth2 and Authentik](../how-to/authentication.md).
