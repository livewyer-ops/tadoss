# TADOSS operator

The controller reconciles `tadoss.livewyer.io/v1alpha1`, kind `Tadoss`. Generated
CRDs and RBAC live in `config/` and the Helm chart; `make generate` updates both.

For direct installation, install CloudNativePG first if using managed databases.
Use an API and operator image built from the same revision:

```bash
kubectl apply --server-side -f deploy/charts/tadoss-operator/crds
helm upgrade --install tadoss-operator deploy/charts/tadoss-operator \
  --namespace tadoss-system --create-namespace \
  --set image.repository=YOUR_REGISTRY/tadoss-operator --set image.tag=YOUR_TAG --wait
```

Run these commands from the repository root. Helm installs CRDs initially but
does not upgrade them, hence the separate apply step. Installing the chart
requires cluster-level permissions for CRDs, RBAC and the native Kubernetes
ValidatingAdmissionPolicy. No custom admission webhook server is required.

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
`replicas`, `database.instances`, `database.storageClass` and `resources` override
profile defaults. Managed databases use CloudNativePG and PostgreSQL 18.4.
Changing `database.provider` on an existing instance is rejected. The instance
name is limited to 40 characters so child resource names remain valid.

## External PostgreSQL and credentials

Set `database.provider: external` and `database.existingSecret` to a Secret in
the instance namespace. Its `uri` key must contain a PostgreSQL connection URI
with permissions to create and use TADOSS tables. Use a dedicated database;
tables are created in its default schema. CloudNativePG is not required for
external mode. Install its CRD before starting the operator if enabling managed
mode later, then restart the controller to register that watch.

Set `tokenSecret` to a Secret with a `token` key, or omit it to generate a
cryptographically random token in `<instance>-api-token`. Missing or empty
credentials keep Ready false. Credential changes trigger API rollouts;
database URI changes also trigger a new schema Job. Existing Secrets and
external databases are never adopted or deleted by the operator.

## Reconciliation and deletion

The controller applies owned resources with server-side apply and corrects
drift. It refuses to overwrite resources owned by another instance. The schema
Job must complete before API changes are applied. A failed migration sets
`Ready=False` with reason `SchemaFailed`; fix the underlying cause and delete
the failed Job to retry. Changing images creates a new Job and prunes old Jobs
after success. Pin immutable image tags or digests for reliable upgrades.

Readiness requires all configured managed database instances and API replicas.
Status includes `observedGeneration`, conditions, endpoint and Secret names;
it never contains credential values. Multiple API replicas get a disruption
budget and topology spread constraints.

Deletion protection defaults to true. The chart's admission policy rejects
DELETE until it is explicitly disabled; a finalizer also guards cleanup.
For deliberate instance removal:

```bash
kubectl -n tads patch tadoss tadoss --type=merge \
  -p '{"spec":{"deletionProtection":false}}'
kubectl -n tads delete tadoss tadoss
```

`deletionPolicy: Retain` (default) detaches the owned CNPG Cluster and preserves
its database, PVCs and database credentials. API workloads and generated API
tokens are removed. `Delete` allows Kubernetes garbage collection to remove
the owned CNPG cluster and its storage. External databases are always retained.
Retained clusters are not automatically adopted by a replacement instance; use
their application Secret with `provider: external` to reconnect to retained data.
Deleting a namespace or Kind cluster removes resources regardless of this
retention setting. Back up data separately.

## Tests

`task operator:test` checks profile defaults, workload security, migration
identity, credential ownership and retention logic. `task operator:e2e:chainsaw`
uses actual Kubernetes resources, PostgreSQL and API Jobs. It reads the image
from the deployed instance, so Kind tests require no published release image.
Failed tests can leave protected resources behind; their temporary namespaces
can be inspected before deleting the disposable Kind cluster.
