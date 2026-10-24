# Operator

The controller resolves `spec.version` through its embedded release catalogue, merges installation defaults into a working copy, prepares the database, runs a migration Job, then reconciles API and worker Deployments. It records the resolved values and marks the applied defaults revision only after the full rollout completes.

The CR remains the user's intent. Defaults are not written back to its spec. An unknown version or invalid defaults file blocks reconciliation with an explicit condition.

```sh
task operator:generate
task operator:test
task operator:lint
```

Generated CRD, RBAC and installation YAML are committed. Install with the environment flow described in [deployment](../deploy/README.md). The operator owns namespaced workload resources; platform prerequisites are managed separately.
