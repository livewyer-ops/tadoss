# Deployment

`platform/helmfile.yaml.gotmpl` manages shared CNPG, Traefik and cert-manager installations and the optional issuer chart. `operator/` is a native Kustomize installation. Each directory in `environments/` contains:

```text
platform-values.yaml
operator/defaults.yaml
operator/kustomization.yaml
instances/<name>/namespace.yaml
instances/<name>/tadoss.yaml
instances/<name>/kustomization.yaml
kustomization.yaml
```

Create an environment and additional instances:

```sh
task env:init NAME=production PROFILE=multi-server DOMAIN=tads.example.com VERSION=0.1.0
task instance:init ENV=production INSTANCE=archive NAMESPACE=archive VERSION=0.1.0
task env:apply ENV=production KUBECONFIG=/path/to/cluster.yaml
task env:wait ENV=production KUBECONFIG=/path/to/cluster.yaml
task env:diff ENV=production KUBECONFIG=/path/to/cluster.yaml
task env:assert-clean ENV=production KUBECONFIG=/path/to/cluster.yaml
```

Review generated defaults, TLS, storage, authentication and backups before applying. Set prerequisite `enabled: false` values when another team owns them. An instance uses an explicit field first, then installation defaults, then its profile. Installation defaults are hashed immutable ConfigMaps; changing them rolls out the operator.

Review source files and `kubectl kustomize` output before a first installation. Helmfile sync installs prerequisites in dependency order, so issuer resources are applied after their CRDs. Once prerequisites exist, use `env:diff` before subsequent applies.

`env:diff` accepts ordinary differences but fails on command errors. `env:assert-clean` also fails on differences. All environment commands use the supplied kubeconfig. `INSTANCE=name` selects one instance. `env:status` and `env:summary` do not read Secrets; `env:credentials` is an explicit credential retrieval command.

## Install with Helmfile and kubectl

Task is optional. After reviewing a checked-in environment, run the underlying tools directly. The example uses the local-kind environment; choose the environment and kubeconfig for your installation.

```sh
export KUBECONFIG=/path/to/cluster.yaml
helmfile --file deploy/platform/helmfile.yaml.gotmpl \
  --state-values-file deploy/environments/local-kind/platform-values.yaml sync
kubectl apply --server-side -k deploy/environments/local-kind/operator
kubectl rollout status deployment/tadoss-operator -n tadoss-system --timeout=600s
kubectl wait --for=condition=Established crd/tadosses.tadoss.livewyer.io --timeout=60s
kubectl apply --server-side -k deploy/environments/local-kind
kubectl wait --for=condition=Ready tadoss/tadoss -n tads --timeout=600s
```

For a subsequent apply, review `helmfile diff --detailed-exitcode` with the same file arguments, then `kubectl diff --server-side -k` for the operator and instance directories. A Ready condition can describe the previous rollout; confirm `status.observedGeneration` equals `metadata.generation` and `status.appliedDefaultsRevision` equals the SHA256 of `operator/defaults.yaml`. `task env:wait` performs both checks.

The [managed example](examples/managed.yaml) includes authentication, TLS, resources and backups. The [external database example](examples/external.yaml) connects to a separately managed database. Provision the referenced resources before applying these examples.

## Install a digest-pinned release

Verify the release checksums and signatures, then use its `install.yaml` as the resource in an operator Kustomize overlay. The release manifest pins the operator digest and that operator embeds the runtime digest. To pin an operator while creating an environment from this checkout:

```sh
task env:init NAME=production PROFILE=multi-server DOMAIN=tads.example.com VERSION=0.1.0 \
  OPERATOR_IMAGE='registry.example/tadoss-operator@sha256:<64-hex-digest>'
```

Replace the digest placeholder before running. A registry port is supported, for example `registry.example:5000/team/operator:v0.1.0`. Images must have an explicit tag or a full SHA256 digest.

To use a downloaded release manifest in a generated environment, copy it to `operator/install.yaml`, change that overlay's `resources` to `[install.yaml]`, and remove its `images` override so the verified manifest selects the operator. Keep `configMapGenerator` and the mount patch.

A native `install.yaml` alone supplies no installation profile or domain. Either retain the generated defaults ConfigMap and mount patch in your release overlay, or set `spec.profile` and the desired ingress/domain fields on every instance. A minimal instance containing only `spec.version` needs installation defaults. Do not install the source operator alongside a release overlay; they name the same resources.

## Clean up old installation defaults

Generated ConfigMaps carry `app.kubernetes.io/name=tadoss-instance-defaults`. They are immutable and deliberately retained for rollout history. List them with:

```sh
kubectl -n tadoss-system get configmap -l app.kubernetes.io/name=tadoss-instance-defaults
kubectl -n tadoss-system get deployment,replicaset,pod -o yaml
```

Before deleting a specific old ConfigMap, check all Deployment and ReplicaSet pod templates and live Pods for references in volumes, projected volumes, `envFrom` and `env.valueFrom`. Keep every referenced revision, including ReplicaSets with zero replicas that may be used for rollback. Remove an unreferenced ConfigMap by its exact name after the rollout is stable. No automatic pruning runs.
