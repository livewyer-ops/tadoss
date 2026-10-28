# Configuration

## Instance configuration

`spec.version` is required and must match an embedded catalogue entry. Explicit image overrides are intended for development and require the same schema target. Full references, including `repository@sha256:digest`, are accepted.

| Field | Purpose |
| --- | --- |
| `profile` | `local-kind`, `single-server`, or `multi-server` |
| `publicEndpoint.baseDomain` | Derives `api.<instance>.<namespace>.<domain>` |
| `api`, `worker` | Image, replicaCount, resources, env/envFrom, scheduling, pod/container security, volumes, probes and disruption budget |
| `backends.db.providedBy` | `cnpg` or `external`; immutable after creation |
| `backends.db.cnpg` | Image, instances, storage, resources, backup and new-instance restore |
| `backends.db.external` | Existing Secret with `uri`; optional CA ConfigMap key |
| `auth` | API token Secret, Basic credential Secret and OAuth2 verification settings |
| `ingress` | Host, className, TLS Secret, clusterIssuer and annotations |
| `networkPolicy` | Enable and supply native API/worker ingress and egress rules |
| `paused` | Stop ordinary resource reconciliation; deletion handling still runs |
| `schemaRetryNonce` | Change after fixing a failed schema Job to request another attempt |
| `deletionPolicy`, `deletionProtection` | Retain or delete managed data; protect against accidental instance deletion |

The generated [CRD](../../operator/config/crd/bases/tadoss.livewyer.io_tadosses.yaml) is the complete field reference. Instance names are DNS labels starting with a letter and at most 40 characters. Explicit resource requests cannot exceed explicit limits. Implicit requests are clamped to explicit limits; implicit limits grow to explicit requests.

Certificates requested through `clusterIssuer` default to subject organization `TADOSS`, so self-signed certificates have a nonempty issuer name. Override it with the native `cert-manager.io/subject-organizations` ingress annotation when your issuer requires a different subject.

Multi-server defaults to two API and worker replicas and three database instances. Other profiles default to one each. Network policies default on for multi-server; their enforcement requires a compatible CNI. Default egress permits DNS, PostgreSQL and HTTP(S); tighten destination rules for your installation.

## Runtime settings

[settings.py](../../src/app/tadoss/settings.py) defines all `TADOSS_` settings and defaults. Each accepts a `_FILE` alternative; setting both is an error. Invalid configuration reports the setting name without its value.

| Setting | Default / requirement |
| --- | --- |
| `TADOSS_DATABASE` | Required outside development mode |
| `TADOSS_DATABASE_SSL_ROOT_CERT` | CA path; forces `sslmode=verify-full` |
| `TADOSS_DATABASE_HOST` | Optional host override preserving URI credentials and TLS |
| `TADOSS_METRICS_TOKEN` | Optional bearer credential valid only on `/metrics` |
| `TADOSS_API_TOKEN` | Query token authentication |
| `TADOSS_BASIC_AUTH_USERNAME`, `TADOSS_BASIC_AUTH_PASSWORD` | Username defaults to `tadoss`; password enables Basic |
| `TADOSS_OAUTH2_ISSUER`, `TADOSS_OAUTH2_AUDIENCE`, `TADOSS_OAUTH2_JWKS_URI` | All required to enable JWT bearer verification |
| `TADOSS_POOL_MIN_SIZE`, `TADOSS_POOL_MAX_SIZE` | 1 / 10 per process |
| `TADOSS_POOL_TIMEOUT` | 30 seconds for request admission and for acquiring a database connection |
| `TADOSS_MAX_INFLIGHT_REQUESTS` | 100 per API process; excess requests receive 503 with `Retry-After: 1` before authentication or body buffering |
| `TADOSS_BODY_READ_TIMEOUT` | 10 seconds for the entire request body; expiry returns 408; the size limit remains 1 MiB |
| `TADOSS_STATEMENT_TIMEOUT` | 30s; applied locally on every application transaction, including transaction pooling |
| `TADOSS_DELETE_BATCH_SIZE`, `TADOSS_DELETE_SYNC_SECONDS` | 1000 / 1 second before deferred work |
| `TADOSS_WORKER_LEASE_SECONDS` | 60 seconds |
| `TADOSS_WORKER_QUEUE_RETENTION_SECONDS` | 604800; zero disables terminal-job expiry |
| `TADOSS_WEBHOOK_MAX_ATTEMPTS` | 5 |
| `TADOSS_WEBHOOK_ALLOWED_HOSTS` | Empty; explicit comma-separated private destination exceptions |
| `TADOSS_DEV` | false; true permits local anonymous access and a local database default |

Typed Kubernetes fields own their corresponding environment variables. Secret and CA object revisions trigger workload rollouts. Configure the same worker settings on API and worker when changing queue behaviour. PostgreSQL JSONB cannot store NUL or lone surrogate characters; writes reject them with a client error, independently for each event in a batch.

The API bounds total in-flight requests before authentication and body buffering. Health and readiness probes and authenticated metrics scraping bypass this HTTP limit. Within that bound, it admits up to `TADOSS_POOL_MAX_SIZE` database requests per process. Further requests wait asynchronously and receive HTTP 503 with `Retry-After` if database admission times out. Authentication and input parsing precede database admission. Admitted synchronous work finishes its transaction before releasing capacity, including when the request is cancelled. Size database pools across all API and worker replicas against PostgreSQL's connection limit, reserving connections for probes, migration and administration. Increasing HTTP capacity does not increase database capacity; it permits more queued requests and buffered bodies. Configure these settings through the existing API workload `env` fields.

JWT verification requires `exp`, `sub`, `iss` and `aud`; the nonempty `sub` is the identity. Scopes may be a space-separated string or a list of strings in `scope`, with `scp` used only when `scope` is absent. Other scope values grant no permissions.

## Defaults and status

| Field | Effective default |
| --- | --- |
| `version` | Required; currently `0.1.0-rc2` |
| `profile` | Installation default; otherwise required on the instance |
| `api.image` / `worker.image` | Release catalogue / resolved API image |
| `api.imagePullPolicy`, `worker.imagePullPolicy` | `IfNotPresent` |
| `api.replicaCount`, `worker.replicaCount` | 2 for multi-server, otherwise 1; zero stops that component |
| `api.resources`, `worker.resources` | Requests 100m CPU and 128Mi memory; limits 1 CPU and 512Mi |
| `backends.db.providedBy` | `cnpg`; immutable |
| `backends.db.cnpg.instances` | 3 for multi-server, otherwise 1; allowed 1 through 5 |
| `backends.db.cnpg.storage`, `storageClass` | 5Gi; cluster default storage class |
| `backends.db.cnpg.image` | Pinned PostgreSQL 18.4 standard image; system variant when backup or restore is enabled |
| `backends.db.cnpg.resources` | Requests 100m CPU and 128Mi; limits 1 CPU and 1Gi |
| `backends.db.cnpg.backup.enabled`, `restore.enabled` | false |
| `auth.tokenEnabled` | `true`; set `false` with OAuth2 or Basic to disable query tokens |
| `auth.oauth2.clientCredentialsSecret` | Optional same-namespace Secret for access tooling; keys `client_id`, `client_secret`, `token_endpoint`, `scopes` |
| `auth.tokenSecret` | Generated `<instance>-api-token`; key `token` |
| `auth.basicSecret` | Unset; keys `username` and `password` when supplied |
| `auth.oauth2.algorithms` | `RS256` |
| `ingress.host` | Derived from base domain if available; otherwise no Ingress |
| `ingress.className` | Installation default; otherwise omitted to use the Kubernetes default |
| `ingress.tlsSecretName` | `<instance>-api-tls` when a cluster issuer is configured |
| `networkPolicy.enabled` | true on multi-server, otherwise false |
| `paused`, `schemaRetryNonce` | false / empty |
| `deletionPolicy`, `deletionProtection` | `Retain` / true |

`status.resolved` records the effective profile, images, schema target and defaults revision. `pendingVersion` records the selected release before rollout and survives failures and operator restarts. `currentVersion` and `appliedDefaultsRevision` advance after the rollout, which clears `pendingVersion`. `observedGeneration` advances when that generation is ready or has failed; inspect `phase` and conditions as well. `endpoint`, `databaseSecret`, `tokenSecret` and `schemaJob` identify generated resources without exposing credentials.

| Condition | Meaning and useful reasons |
| --- | --- |
| `Ready` | Overall rollout; `Available`, `Paused`, `ConfigurationInvalid`, `DatabasePending`, `DatabaseSecretMissing`, `DatabaseSecretInvalid`, `DatabaseCAMissing`, `DatabaseCAInvalid`, `SchemaPending`, `SchemaFailed`, `RolloutPending`, `DeletionBlocked`, `RestoreRequiresNewInstance`, `ReconcileFailed` |
| `DatabaseReady` | Managed database Pods are ready on the selected image and URI/CA inputs are available; an external database is not probed by the controller |
| `SchemaMigrated` | The selected schema Job completed |
| `APIReady`, `WorkerReady` | All replicas match the current Deployment generation and are available |
| `BackupPolicyReady` | CNPG has reported a successful backup; `NotConfigured` when disabled; absent for external databases |

Missing token, Basic or workload Secret inputs appear as `TokenSecretMissing`, `TokenSecretInvalid`, `BasicSecretInvalid` or `ReferencedSecretMissing` on `Ready`. Conditions are reconciliation progress; backup readiness does not gate the API becoming Ready. See [operations](../how-to/operations.md) for recovery actions.

## Database TLS and worker limits

Managed clients mount `<instance>-db-ca` key `ca.crt` and set `TADOSS_DATABASE_SSL_ROOT_CERT=/etc/tadoss/database-ca/ca.pem`. API, worker and schema Job all use certificate and hostname verification. For an external database, supply `external.caConfigMap` with the CA key; without it, TLS follows the URI supplied by its owner.

| Setting | Default / behavior |
| --- | --- |
| `TADOSS_DELETE_MAX_ATTEMPTS` | 5 consecutive failed batches; a committed successful batch resets the count |
| `TADOSS_WORKER_POLL_INTERVAL_SECONDS` | 5; sleep only when idle or after a database failure; maintenance uses this cadence |
| `TADOSS_WORKER_MAX_REQUESTS` | 50 jobs per batch; a full batch is followed immediately by another |
| `TADOSS_WORKER_HEALTH_PORT` | 8001; health, readiness and metrics |
| `TADOSS_WEBHOOK_TIMEOUT_SECONDS` | 10; total TCP, TLS, send and response-header budget after DNS validation |
| `TADOSS_WEBHOOK_MAX_ATTEMPTS` | 5 attempts per delivery; independent of deletion failures |

DNS resolution uses the platform resolver and is outside the webhook deadline. All resolved addresses are checked before connecting. Connection failures try the next validated address within the same deadline; requests are sent only once per attempt. A receiver that keeps sending small header chunks cannot extend the socket budget. Delivery uses the HTTP status and closes the response without reading its body. Configure a suitable resolver timeout at the platform level.

The worker serves `GET` and `HEAD` on `/healthz`, `/readyz` and `/metrics`. Health and readiness return short plain-text responses; HEAD omits the body. Readiness requires a successful database/schema check, and both probes fail when progress is stale or the worker shuts down. Unknown paths return 404. Failure to bind the health port stops worker startup.
