# Troubleshooting

Start with `task env:status` and the instance conditions: `DatabaseReady`, `SchemaMigrated`, `APIReady`, `WorkerReady` and `BackupPolicyReady`. `Ready` requires complete rollouts, including removal of old replicas. `status.resolved` shows the selected release and defaults revision; `appliedDefaultsRevision` changes after the rollout succeeds.

For `ConfigurationInvalid`, correct the CR or installation defaults. For a failed schema Job, inspect its logs privately, fix the cause, then change `spec.schemaRetryNonce`. Setting `paused: true` suspends ordinary reconciliation while retaining deletion protection and finalizer handling.

API `/healthz` checks the process; `/readyz` checks the database and migration set. Worker probes use port 8001. API `/metrics` requires authentication; worker metrics are cluster-internal on port 8001. Metrics label request templates and queue kinds, never resource IDs or credential values. OpenTelemetry is optional and requires the `otel` extra and an OTLP endpoint.

Create a restricted support bundle:

```sh
uv run --project src python scripts/support_bundle.py --kubeconfig /path/to/cluster.yaml --namespace tads --output reports/support.json
```

The bundle allowlists resource names and selected status/condition fields. It excludes Secrets, specs, annotations, logs and free-form error messages. Review it before sharing. Collect logs separately only when needed and remove credentials, URLs containing tokens and customer payloads.

## Slow namespace deletion

CNPG database pods have a long shutdown grace period. Kubernetes can defer namespace cleanup based on that period even when the pods finish sooner. For disposable environments, delete the instance and wait for its database pods to disappear before deleting the namespace. With `deletionPolicy: Retain`, the database remains until you explicitly remove it; do that only when its data is no longer needed.
