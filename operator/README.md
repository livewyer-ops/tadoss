# TADOSS operator

The controller reconciles `tadoss.livewyer.io/v1alpha1`, kind `Tadoss`. Generated
CRDs and RBAC live in `config/` and the Helm chart; `make generate` updates both.

For direct installation, install CloudNativePG first if using managed databases.
Use an API and operator image built from the same revision:

```bash
helm upgrade --install tadoss-operator deploy/charts/tadoss-operator \
  --namespace tadoss-system --create-namespace \
  --set image.repository=YOUR_REGISTRY/tadoss-operator --set image.tag=YOUR_TAG --wait
kubectl create namespace tads
kubectl apply --server-side -k deploy/instances/base
```

Run these commands from the repository root. The CRD is a chart template annotated
`helm.sh/resource-policy: keep`, so `helm upgrade` upgrades it and `helm uninstall`
leaves it in place; set `crds.install=false` when another release owns it. Installing
the chart requires cluster-level permissions for CRDs, RBAC and the native Kubernetes
ValidatingAdmissionPolicy. No custom admission webhook server is required.

## Chart values

`values.schema.json` rejects unknown keys. The main values:

| Value | Default | Purpose |
| --- | --- | --- |
| `image.repository`, `image.tag`, `image.pullPolicy` | `livewyer/tadoss-operator`, chart `appVersion` | Operator image |
| `imagePullSecrets` | `[]` | Pull Secrets for a private registry |
| `watchNamespaces` | `[]` | Reconcile only these namespaces; renders Roles instead of a ClusterRole |
| `serviceAccount.create`, `.name`, `.annotations` | `true`, release name | Identity of the manager |
| `rbac.create` | `true` | Render the generated rules |
| `leaderElection.enabled` | `true` | Lease in the release namespace |
| `logging.level`, `logging.encoder` | `info`, `json` | Controller logging |
| `metrics.enabled`, `.port`, `.secure` | `true`, `8443`, `true` | Metrics endpoint and its authentication |
| `metrics.serviceMonitor.enabled`, `.labels` | `false` | Prometheus Operator scraping |
| `deletionProtection.enabled`, `.namespaceSelector`, `.exemptUsers` | `true`, `{}`, `[]` | Admission policy scope |
| `networkPolicy.enabled`, `.metricsFrom`, `.extraEgress` | `false` | Isolate the manager pod |
| `podDisruptionBudget.enabled`, `.minAvailable` | `false`, `1` | For `replicas` above one |
| `resources`, `nodeSelector`, `tolerations`, `affinity`, `topologySpreadConstraints`, `priorityClassName`, `podAnnotations`, `extraArgs` | | Scheduling and pod details |

Metrics are served over HTTPS and require a bearer token whose subject may `get`
the `/metrics` non-resource URL; bind the rendered
`<namespace>-<release>-metrics-reader` ClusterRole to the scraper's
ServiceAccount, or set `metrics.secure=false` on a trusted network. The
NetworkPolicy allows probes, metrics from `networkPolicy.metricsFrom`, DNS and
the Kubernetes API; Kind's default network plugin does not enforce policies.

```yaml
apiVersion: tadoss.livewyer.io/v1alpha1
kind: Tadoss
metadata:
  name: tadoss
  namespace: tads
spec:
  profile: multi-server
  image: YOUR_REGISTRY/tadoss-api:YOUR_TAG
  database:
    provider: managed
    storage: 20Gi
  deletionProtection: true
  deletionPolicy: Retain
  ingress:
    host: data.example.com
    className: traefik
    tlsSecretName: tadoss-tls
    clusterIssuer: tadoss-public
```

Create the namespace first. Omit `ingress` for Service-only access. Optional
`replicas`, `database.instances`, `database.storageClass`, `database.image`,
`database.resources` and `resources` override profile defaults. Managed databases
use CloudNativePG and PostgreSQL 18.4 unless `database.image` names another
operand. `ingress.annotations` are added to the Ingress for class-specific
behaviour such as redirects; the cert-manager issuer annotation always wins.
The operator owns the Deployment's replica count, so autoscalers are reverted.

The CRD rejects invalid combinations at admission: `ingress.clusterIssuer`
requires `ingress.tlsSecretName`, `database.existingSecret` is required with
`provider: external` and rejected with `managed`, `ingress.className` and Secret
names must be DNS names, and `database.provider` cannot change on an existing
instance. The instance name is limited to 40 characters so child resource names
remain valid.

## External PostgreSQL and credentials

Set `database.provider: external` and `database.existingSecret` to a Secret in
the instance namespace. Its `uri` key must contain a PostgreSQL connection URI
with permissions to create and use TADOSS tables and to create the trusted
`btree_gist` extension; owning the database is sufficient. Use a dedicated database;
tables are created in its default schema. CloudNativePG is not required for
external mode. Install its CRD before starting the operator if enabling managed
mode later, then restart the controller to register that watch.

Set `tokenSecret` to a Secret with a `token` key, or omit it to generate a
cryptographically random token in `<instance>-api-token`. Missing or empty
credentials keep Ready false. Credential changes trigger API rollouts;
database URI changes also trigger a new schema Job. Existing Secrets and
external databases are never adopted or deleted by the operator. The controller
reads credential Secrets directly from the API server and keeps only Secret
metadata in its cache; Secret contents are never cached. On shared clusters set
the chart value `watchNamespaces` so the cache and the operator's RBAC cover
only those namespaces: the chart then renders a Role per namespace instead of a
ClusterRole, keeping only namespace reads cluster-wide.

## Reconciliation and deletion

The controller applies owned resources with server-side apply and corrects
drift. It refuses to overwrite resources owned by another instance. Workload
selectors use the instance name rather than its UID, so an instance recreated
with the same name adopts the API Deployment that survived it; an operator
upgrade recreates a Deployment that still carries the old selector once, keeping
its pods running. The schema
Job must complete before API changes are applied. A failed migration sets
`Ready=False` with reason `SchemaFailed`; fix the underlying cause and delete
the failed Job to retry. The migration waits up to five minutes for the database,
which covers a PostgreSQL restart triggered by the same change, and the Job is
allowed fifteen minutes in total. Changing the image, the database credentials or the
API `resources` creates a new Job and prunes old Jobs after success. Pin
immutable image tags or digests for reliable upgrades.

Readiness requires all configured managed database instances and API replicas.
Status includes `observedGeneration`, conditions, endpoint and Secret names;
it never contains credential values. `observedGeneration` reaches
`metadata.generation` only once that generation is rolled out or has failed, so
wait on it and then on the `Ready` condition. An emptied generated token Secret
is refilled; a Secret named by `tokenSecret` is never modified. Multiple API replicas get a disruption
budget and topology spread constraints.

Deletion protection defaults to true. The chart's admission policy rejects
DELETE until it is explicitly disabled; a finalizer also guards cleanup and
requeues a blocked deletion every minute. For deliberate instance removal:

```bash
kubectl -n tads patch tadoss tadoss --type=merge \
  -p '{"spec":{"deletionProtection":false}}'
kubectl -n tads delete tadoss tadoss
```

The chart value `deletionProtection.enabled` (default `true`) installs the
policy and its binding, `deletionProtection.namespaceSelector` limits the
binding to matching namespaces, and `deletionProtection.exemptUsers` lists
usernames the policy never blocks. Break glass with
`kubectl delete validatingadmissionpolicybinding <namespace>-<release>-deletion-protection`
(`tadoss-system-tadoss-operator-deletion-protection` for the default install);
the finalizer still requires `deletionProtection: false`.

`deletionPolicy: Retain` (default) detaches the owned CNPG Cluster and preserves
its database, PVCs and database credentials. API workloads and generated API
tokens are removed. `Delete` allows Kubernetes garbage collection to remove
the owned CNPG cluster and its storage. External databases are always retained.
Retained clusters are not automatically adopted by a replacement instance; use
their application Secret with `provider: external` to reconnect to retained data.
Namespace deletion bypasses both guards: the policy never blocks the namespace
controller or a request against a terminating namespace, and the operator
releases the finalizer as soon as the namespace carries a deletion timestamp,
whatever `deletionProtection` and `deletionPolicy` say. The PostgreSQL cluster
and its storage are then garbage collected with the namespace even under
`Retain`. If the operator is not running, the namespace stays `Terminating`
until the operator returns. Back up data before deleting a namespace or a Kind
cluster.

## Tests

`task operator:test` checks profile defaults, workload security, migration
identity, credential ownership, retention logic and finalizer release on
namespace termination. `task operator:e2e:chainsaw`
uses actual Kubernetes resources, PostgreSQL and API Jobs. It reads the image
from the deployed instance, so Kind tests require no published release image.
Failed tests can leave protected resources behind; their temporary namespaces
can be inspected before deleting the disposable Kind cluster.
