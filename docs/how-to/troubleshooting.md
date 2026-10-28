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

## API saturation

`tadoss_http_inflight` counts requests admitted before body buffering, including the metrics scrape itself. `tadoss_admission_rejections_total{stage="http"}` counts immediate capacity rejections; `stage="database"` counts database-admission timeouts. Both return 503 with `Retry-After`. A 408 indicates the total body-read deadline expired. Health/readiness bypass HTTP admission and the shared request thread pool. Readiness permits one database probe at a time, returning 503 to additional probes; database pressure can also make it unavailable.

`tadoss_database_active`, `tadoss_database_waiting` and `tadoss_database_admission_seconds` separate work holding admission from queued work. `tadoss_database_pool_connections` (states `size`, `available`, `maximum`) and `tadoss_database_pool_waiting` report the underlying pool once per second. `tadoss_event_loop_lag_seconds` samples scheduling delay at the same interval. Metrics remain process-local with bounded labels.

```promql
sum by (stage) (rate(tadoss_admission_rejections_total[5m]))
histogram_quantile(0.95, sum by (le) (rate(tadoss_database_admission_seconds_bucket[5m])))
histogram_quantile(0.95, sum by (le) (rate(tadoss_event_loop_lag_seconds_bucket[5m])))
```

## Event and webhook metrics

An HTTP 200 event-batch response can contain rejected records. `tadoss_events_total` counts each result from batches whose database transaction committed, with `outcome` set to `created`, `updated` or `rejected`. Failed transactions contribute no event outcomes. `tadoss_event_batch_size` records submitted batch sizes after request admission.

Use these PromQL queries with the API and worker metrics already scraped by your monitoring system:

```promql
sum by (outcome) (rate(tadoss_events_total[5m]))
sum(rate(tadoss_event_batch_size_sum[5m])) / sum(rate(tadoss_event_batch_size_count[5m]))
sum by (outcome) (rate(tadoss_webhook_delivery_total[5m]))
histogram_quantile(0.95, sum by (le) (rate(tadoss_webhook_delivery_seconds_bucket[5m])))
```

Webhook outcomes are `success`, `http_error`, `transport_error`, `rejected` or `internal_error`. Each retry is a separate attempt. Disabled or superseded subscriptions skipped before delivery add no attempt. A successful delivery records the receiver's response; lease expiry can still prevent completion from being stored, so inspect queue age and retry activity together. These process-local metrics are diagnostic counters, not an audit ledger. They contain no resource IDs or destination URLs.
