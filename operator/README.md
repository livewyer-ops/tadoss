# Operator

The controller resolves `spec.version` through its embedded release catalogue, merges installation defaults into a working copy, prepares the database, runs a migration Job, then reconciles API and worker Deployments. It records the resolved values and marks the applied defaults revision only after the full rollout completes.

The CR remains the user's intent. Defaults are not written back to its spec. An unknown version or invalid defaults file blocks reconciliation with an explicit condition.

`internal/controller/tadoss_controller.go` orders the reconciliation stages. Database, credentials, schema and workloads have focused files in the same package. Pod configuration is shared by the migration, API and worker. Apply/prune, deletion, status and watches are separate from the stage order. Installation defaults and release selection live in their existing packages.

## Common operator code

Resource application, namespace scoping, primary-resource event filtering, CNPG rollout checks, condition updates and environment rendering use implementations from [TAMOSS](https://github.com/livewyer-ops/tamoss/tree/25ff4d58f89592dd63d1e8bc019d447acb6acf7e/operator/internal). Carry fixes to these implementations across both projects. `internal/controller/managed_apply.go` differs only in its module import; namespace, condition and environment helper function bodies are shared unchanged. Reference watches use the same controller-runtime field-index pattern.

Managed apply removes externally added fields from operator-owned resource specifications while preserving Service allocations, status writers and unrelated metadata. Ownership checks run before apply, so an existing resource belonging to another instance is rejected. The cache retains managed field ownership; Secret and ConfigMap contents remain uncached.

TADOSS owns its release catalogue, explicit schema Job, worker configuration and database retention policy. CNPG readiness requires the desired number of owned database Pods running the selected image and reporting ready. Schema Jobs wait for that rollout.

## Development

```sh
task operator:generate
task operator:test
task operator:lint
```

For a focused check, run `go test ./internal/controller/defaults` from `operator`. Controller tests need `make test`, which installs the pinned envtest API server and runs all packages with the race detector. `make fmt` runs gofmt and goimports; `make lint` checks both formatting and Go diagnostics. Run `task check` before submitting a change.

Generated CRD, RBAC and installation YAML are committed. Install with the environment flow described in [deployment](../deploy/README.md). The operator owns namespaced workload resources; platform prerequisites are managed separately.
