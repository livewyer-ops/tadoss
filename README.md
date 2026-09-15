# TADOSS

**Time Addressable Data Open Source Store** implements the core
[BBC TADS 0.1 API](https://github.com/bbc/tads): JSON events on media timelines,
JSON Schema validation and nanosecond time queries. PostgreSQL stores the data.
Tracks link to [TAMS](https://github.com/bbc/tams) and
[TAMOSS](https://github.com/livewyer-ops/tamoss) media through `tams://` references.

The deployment follows TAMOSS: a Go Kubernetes operator, Helm and Helmfile,
Kustomize instances, Kind environments and native Chainsaw integration tests.
The API and specification are alpha. Webhook delivery is not implemented;
TADOSS does not yet claim full TADS conformance.

## Kind quick start

Install Docker and [aqua](https://aquaproj.github.io/docs/install/), then:

```bash
aqua install
task setup
task kind:up
export KUBECONFIG="$PWD/.local/tadoss.kubeconfig"
kubectl -n tads get tadoss,pods
```

`task setup` creates the Python environment and installs the pinned helm-diff plugin
into `.local/helm/plugins`. `task kind:up` builds both images, tags them with a hash of
their sources, loads them into the `tadoss` Kind cluster and runs helmfile: CloudNativePG,
Traefik, cert-manager, a self-signed ClusterIssuer, the operator chart (CRD, controller and
native deletion admission policy) and a `Tadoss` resource. The operator then provisions
PostgreSQL, an isolated schema migration Job, the API, its generated bearer-token Secret,
Service and TLS ingress.

Open <https://api.tadoss.localtest.me:18443/docs>. The local certificate is
self-signed. HTTP uses port 18080; both ports bind only to loopback. The hostname
resolves to loopback. To use the API with a client:

```bash
export TADOSS_TOKEN="$(kubectl -n tads get secret tadoss-api-token -o jsonpath='{.data.token}' | base64 --decode)"
kubectl -n tads port-forward service/tadoss-api 8000:8000
# In another terminal, with the same token exported:
python3 examples/transcript.py
```

The example reads `TADOSS_TOKEN` and defaults to `http://127.0.0.1:8000`. Set
`TAMS_ENTITY` to a real `tams://host/sources/<uuid>` or flow URI. It stores that
reference without contacting the media store.

```bash
task operator:e2e:chainsaw  # Test the running cluster
task kind:test             # Build, deploy, confirm idempotence and test
task kind:down             # Delete this disposable cluster, including its data
```

## Profiles and environments

A profile sizes an instance: it is the `spec.profile` value of the `Tadoss` resource. An
environment is a cluster: a file in `deploy/environments` whose values merge over
`deploy/values/defaults.yaml`.

| Profile | API replicas | Managed PostgreSQL instances | Kind nodes |
| --- | ---: | ---: | ---: |
| `local-kind` | 1 | 1 | 1 |
| `edge` | 1 (64 MiB memory request) | 1 | 1 |
| `single-server` | 1 | 1 | 1 |
| `multi-server` | 2 | 3 | 4 (one control plane, three workers) |

Select a profile with `task kind:up PROFILE=multi-server`. A pre-existing
single-node cluster cannot become a four-node cluster in place: remove only
that disposable cluster with `task kind:down` first. Built-in Kind configurations
share host ports, so run one at a time. Kind validates deployment behaviour;
its nodes still share one physical host.

For an existing Kubernetes cluster (1.32+, a default StorageClass required for
managed PostgreSQL), create an environment from the template:

```bash
task env:init NAME=production DOMAIN=data.example.com
```

Edit `deploy/environments/production.yaml`. Set `tls.acme.email`, and set `images.tag`
to API and operator images published to your registry; this repository publishes no
release images yet. `tls.acme.server` starts on the Let's Encrypt staging directory;
switch it to `production` once a certificate issues. Point the domain at Traefik's
external address and make ports 80 and 443 reachable. Preview, apply and wait with the
intended kubeconfig:

```bash
task env:diff ENV=production KUBECONFIG=/path/to/cluster.kubeconfig
task env:apply ENV=production KUBECONFIG=/path/to/cluster.kubeconfig
task env:wait ENV=production KUBECONFIG=/path/to/cluster.kubeconfig
task env:summary ENV=production KUBECONFIG=/path/to/cluster.kubeconfig
```

`platform.cnpg.enabled`, `platform.traefik.enabled` and `platform.certManager.enabled`
leave a component out when the cluster already runs it; a release that something else
installed is neither adopted nor removed. Use `tls.mode: existing` with your own
ClusterIssuer or TLS Secret and set `instance.spec.ingress` to match. Platform
controllers are cluster-wide; review values before applying into a shared cluster.
Removing the instance release is denied while `deletionProtection` is true. See
[operator usage](operator/README.md) for existing databases, credentials, retention and
direct Helm installation, and the [decision records](docs/decisions/README.md) for the
reasoning behind this layout.

## Local development

```bash
aqua install
task setup
task dev
```

`task dev` starts PostgreSQL with Compose on loopback port 15432 and the API on
<http://127.0.0.1:8000/docs>. It explicitly enables unauthenticated development
mode. For a containerized API with authentication:

```bash
export TADOSS_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
aqua exec -- docker-cli-plugin-docker-compose --profile app up --build -d --wait
```

`docker-cli-plugin-docker-compose` is the binary installed by aqua; an existing
`docker compose` installation also works. The `tadoss-postgres` Compose volume
retains data across restarts. `down` retains it; `down -v` deletes it.

## API coverage

| Resource | Supported behavior |
| --- | --- |
| `/`, `/service` | Discovery and service metadata |
| `/schemas` | Create, list, retrieve and delete payload schemas; immutable schema contents |
| `/schemas/{id}/...` | Schema retrieval; tags, label, description and external ID operations |
| `/tracks` | Create, replace, retrieve, filter and delete tracks |
| `/tracks/{id}/...` | Tags, label, description and track protection controls |
| `/tracks/{id}/events` | Create/edit batches, list and delete events by time or ID |
| `/track-delete-requests` | Empty listing; all deletions complete synchronously |

GET endpoints also support HEAD. Collection responses use TADS paging headers
and `Link: ...; rel="next"`. Filters support exact labels, tag values/presence,
schema IDs and timeline references where specified upstream.

Event writes return HTTP 200 with `all_successful` and an ordered `responses`
array. Check each `http_status_code`; valid events are committed even when other
events in the batch fail. IDs are unique across the store. Read-only tracks
reject metadata changes and event writes/deletes; `/read_only` can unlock them.
`editable_events=false` prevents replacement but permits deletion and recreation.

Times use the BBC `mediatimestamp` library with nanosecond precision. For example,
`[0:0_1:500000000)` includes the start and excludes 1.5 seconds. `[1:0]` is an
instant; `_` is the entire timeline; `()` is empty. Queries match overlapping
events. Time-filtered deletion removes only events wholly inside the range.
An `edit_rate` requires event boundaries to lie on its rational rate grid.

TAMS timeline references follow
[TADS AppNote 0001](https://github.com/bbc/tads/blob/main/docs/appnotes/0001-using-tads-with-tams.md).
Use `tams://` and prefer a Source URI. Omit `timeline_selected` for TAMS references.
To filter by an entity URI, percent-encode each URI before passing the joined
list as a query parameter; the HTTP client then encodes the parameter itself.

## Configuration and limits

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `TADOSS_TOKEN` | Unset | Shared bearer token; required unless development mode is explicit |
| `TADOSS_DEV` | `false` | Allows unauthenticated local use when no token is set |
| `TADOSS_DATABASE` | `postgresql://tadoss:tadoss@127.0.0.1:15432/tadoss` | PostgreSQL connection URI |
| `TADOSS_AUTO_MIGRATE` | `true` | Initialize schema on startup; the operator disables this and uses a Job |
| `TADOSS_HOST` | `127.0.0.1` | Listen address; container uses `0.0.0.0` |
| `TADOSS_PORT` | `8000` | HTTP port |

`python -m tadoss migrate` initializes or checks the database schema. Kubernetes
API rollouts wait for that Job to succeed. `/readyz` checks database connectivity
and schema compatibility; `/healthz` checks the process.

The shared token grants access to the whole store. URL tokens are accepted for
TADS compatibility; prefer the Authorization header. API access logging is
disabled to keep URL tokens out of logs. Health probes and documentation are
public. OIDC, per-resource permissions and HTTP Basic are not implemented.

Writes are limited to 1 MiB per request and 1,000 events per batch. Pages default
to 100 items and cap at 1,000. PostgreSQL transactions permit multiple API
replicas; a database advisory lock currently serializes all writes. Listings
and overlap checks scan records in memory. Offset pagination can shift under
concurrent edits. Indexed range queries and finer locks are needed for higher
throughput. The operator does not configure database backups: configure and
verify CNPG backups or your external provider's backup policy before storing
irreplaceable data. Retention is not a backup.

Payload schemas default to JSON Schema Draft 2020-12 when `$schema` is absent.
Other dialects supported by `jsonschema` can be declared explicitly. References
must resolve within the supplied schema; remote retrieval is disabled. `format`
is an annotation. Timestamps must fit the BBC library's 48-bit seconds range.

The upstream `allow_overlaps` descriptions contradict its boolean meaning in
places. TADOSS rejects switching it to **false** when overlaps exist, consistent
with the track/event constraints. Changing a populated track's timeline
reference and deleting a referenced schema are rejected.

## Checks

```bash
task check                 # Lint and test everything; starts PostgreSQL with Compose
task fmt                   # Format Python, Go and YAML
task operator:generate     # Regenerate CRD, deepcopy code, chart CRD template and RBAC
task kind:test             # Live Kubernetes integration suite
```

`task check` runs ruff, the API tests, `go vet` and the operator tests with the race
detector, `helm lint` for both charts including the ACME branch, kubeconform over every
rendered manifest (the `Tadoss` resource is validated against a schema generated from its
CRD), Chainsaw lint, yamlfmt and actionlint.

Chainsaw checks provisioning, real API writes, persistence after pod replacement,
drift correction, deletion protection, database retention, external PostgreSQL,
missing credentials and token rotation. JUnit reports are written to
`reports/<profile>/operator-chainsaw.xml`. CI runs these tests on both the one-node
and four-node Kind profiles, with caches for aqua, uv, Go and Helm plugins; Renovate
keeps the pinned tools, charts, images and actions current. Tags matching `v*` build
multi-arch images, scan, sign and publish them with the packaged charts.

The bundled upstream contract is pinned to
[`d2b90e52`](https://github.com/bbc/tads/commit/d2b90e52d54db947f4c8ddab571538ed18ab3052).
The runtime OpenAPI document lists implemented endpoints. Regenerate the bundle
with `uv run --with pyyaml scripts/vendor_spec.py /path/to/bbc-tads`.
Source and contract licenses are recorded in [LICENSE](LICENSE) and [NOTICE](NOTICE).
