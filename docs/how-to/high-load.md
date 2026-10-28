# Multi-server performance qualification

The multi-server reference uses a stateless API, a synchronous CNPG primary with two standbys, and two transaction-mode PgBouncer pods. It is a starting configuration to measure on your hardware. Capacity and latency limits have not been established on dedicated nodes.

These changes require runtime and operator images built from this source revision. Published RC2 images predate them. `task kind:up` builds both images automatically. For a dedicated cluster, build and publish your candidate images, record their digests, and set the operator image and `spec.api.image` to those digests before applying the environment.

## Reference settings

| Component | Starting configuration |
| --- | --- |
| API | One process per pod; CPU HPA, 2–8 replicas, 70% utilisation of the API container's CPU request; 300-second downscale stabilisation |
| API resources | 1 CPU / 1 GiB requests and limits; pool maximum 10; 64 in-flight requests |
| Workers | Two explicit replicas, ten connections each; scale separately after measuring queue age and drain rate |
| PostgreSQL | Three instances on separate nodes; each requests and limits 2 CPU / 8 GiB; 100 GiB initial storage |
| Durability | `synchronous_commit=on`; one synchronous standby chosen from available standbys; `dataDurability=required`; failover quorum enabled |
| Pooler | Two replicas on separate nodes; transaction pooling; 16 server connections and 200 clients per pod; one rollout surge pod |
| PostgreSQL memory | `shared_buffers=2GB`, `effective_cache_size=6GB`, `work_mem=8MB`, `maintenance_work_mem=256MB` |
| Diagnostics | `pg_stat_statements`, I/O timing, WAL I/O timing, Prometheus metrics, statement and admission timeouts |

`work_mem` applies per query operation and parallel worker, so it is not a per-connection cap. `effective_cache_size` is a planner estimate. Recalculate these settings when changing memory limits or query concurrency. Choose a storage class with measured durable write latency and adequate IOPS; replica count cannot compensate for slow storage.

The reference connection budget is `(2 + 1) × 16 + (2 + 1) × 10 + 20 = 98`, below PostgreSQL's limit of 100. It includes one extra Pooler, the worker Deployment's rounded-up 25% surge, and twenty connections reserved for CNPG, replication, migration, probes and administration. API connections, including its separate readiness connection, pass through the Pooler's server cap. Direct workers and migrations use the primary service. The operator rejects managed pooling configurations that exceed this budget. Keep the worker pool maximum explicit; conflicting `_FILE` settings fail runtime validation. Arbitrary sidecars or external database clients need a separately reviewed reserve.

API transactions use transaction-local statement timeouts. Connection checkout checks discard broken idle connections before application SQL starts; writes are never replayed. PgBouncer enables prepared statement tracking; the runtime's Psycopg binary and libpq must support it. TLS remains `verify-full` through the Pooler, whose DNS names are included in CNPG's server certificate. Pooler disable waits for the API's direct-connection rollout before deleting the old Pooler.

An HPA owns API replicas while enabled. Remove `api.replicaCount` when enabling it; the fields are mutually exclusive. Disable autoscaling and set `replicaCount` to return to fixed capacity. Worker replicas remain explicit. Custom native HPA metrics and behaviour can be supplied, but throughput autoscaling should follow measurement of request mix and database saturation. Kubernetes selects the largest recommendation from multiple metrics; a database metric does not veto CPU-driven scale-out. See [HPA behaviour](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/) and [CNPG pooling](https://cloudnative-pg.io/docs/1.30/connection_pooling/).

Required synchronous replication can pause writes when no required standby is available. Preserve that setting during failure tests. It protects acknowledged commits during the tested failure modes; backups and restore tests remain necessary for wider data-loss scenarios.

## Fresh and shared platforms

Copy `deploy/environments/multi-server` and configure its domain, TLS, storage and candidate images. Its platform values select pinned CNPG 1.30.1, Metrics Server, and kube-prometheus-stack. Metrics Server provides CPU samples to the HPA independently of Prometheus. The reference monitoring stack includes Prometheus Operator, Prometheus, Grafana and Alertmanager. Configure persistent storage and Alertmanager receivers before using it for operations.

When TAMOSS or the platform team already supplies a prerequisite, omit its release from Helmfile by setting the corresponding `platform.*.enabled: false` in the copied `platform-values.yaml`:

```yaml
platform:
  cnpg: {enabled: false}
  traefik: {enabled: false}
  certManager: {enabled: false}
  metricsServer: {enabled: false}
  monitoring: {enabled: false}
tls:
  mode: existing
```

`env:apply` refuses selected pre-existing Helm releases unless they carry the `TADOSS-platform` release description written by this installer. Older unmarked TADOSS releases also require explicit reuse or a separately reviewed platform upgrade. Disabled releases are omitted entirely; TADOSS does not adopt, upgrade or uninstall them. Reused CNPG must support the 1.30 synchronous replication and Pooler API fields. Review shared CRDs and platform versions before applying an instance.

Configure the existing Prometheus discovery labels once in `operator/defaults.yaml`:

```yaml
monitoring:
  enabled: true
  labels:
    release: shared-monitoring
  namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: monitoring
```

The labels must match that Prometheus's `podMonitorSelector` and `ruleSelector`; its namespace selectors must include each TADOSS namespace. `namespaceSelector` above controls worker metrics ingress, not Prometheus discovery. The bundled reference stack uses `monitoring.tadoss.livewyer.io/discovery: enabled` and discovers monitors across namespaces. If you restrict API ingress yourself, also allow the monitoring namespace on port 8000.

The operator creates PodMonitors, alerts and a separate `<instance>-metrics` Secret. The scrape token grants `/metrics` access only. API metrics bypass normal request admission and use a separate bounded thread slot; readiness also has an independent connection. Worker metrics remain internal to the allowed namespaces. Rotating the scrape Secret rolls the API. `MonitoringReady=Configured` means resources exist; qualification checks actual Prometheus targets. Missing monitoring CRDs leave the serving API available and are retried on reconciliation, including when monitoring is installed later.

Import [dashboard.json](../../deploy/monitoring/dashboard.json) into a shared Grafana, or install the dashboard ConfigMap for the reference Grafana sidecar:

```sh
kubectl --kubeconfig /path/to/cluster.yaml apply -k deploy/monitoring
```

The dashboard covers requests, latency, CPU, RSS, throttling, admission waits, queue age, replication and Pooler waiting clients. Alerts cover sustained HTTP errors, database admission waiting, worker backlog and unavailable scrape targets. Review operational thresholds after measuring the workload.

## Disposable qualification

Local Kind checks establish functionality only. Node containers share a host, disk and kernel; they cannot establish production capacity or independent failure domains. `deploy/kind/multi-server-patch.yaml` reduces allocations without weakening synchronous replication or anti-affinity. The Kind-only Metrics Server setting accepts kubelet certificates from the disposable cluster.

```sh
task kind:up CLUSTER=tadoss-perf PROFILE=multi-server HTTP_PORT=28080 HTTPS_PORT=28443
```

Use `scripts/qualify.py` through `task test:cluster`. `prepare` creates a new `qual-*` namespace and stamps both it and the instance as disposable; it refuses an existing namespace. Every load, mutation and fault command checks these labels. Start from a Tadoss manifest with candidate images, suitable resource allocations and valid TLS configuration. The operator and shared platform must already be installed.

```sh
task test:cluster -- prepare --kubeconfig /path/to/cluster.yaml --namespace qual-api --manifest /path/to/candidate-tadoss.yaml
```

Configure the instance's OAuth issuer and `clientCredentialsSecret` as described in [configuration](../reference/configuration.md), or install the synthetic qualification issuer/receiver. The synthetic fixture exercises HTTPS, JWT verification, scope checks and worker delivery; it does not measure a production identity provider. Its ingress uses Traefik and a selected ClusterIssuer; the fixture trusts its backend CA without disabling TLS verification.

```sh
task test:cluster -- fixture --kubeconfig /path/to/cluster.yaml --namespace qual-api --fixture-url https://auth.qual-api.example.com --fixture-issuer production-issuer --output reports/qualification/fixture
kubectl --kubeconfig /path/to/cluster.yaml -n monitoring port-forward service/tadoss-monitoring-kube-pro-prometheus 29090:9090
```

Run subsequent commands in another terminal. Supply `--api-url` for a host-port override on Kind, and `--ca-file reports/qualification/fixture/ca.pem` for the fixture-generated CA bundle. Kind hostnames ending in `localtest.me` resolve to loopback. On dedicated nodes, use real reachable names and ingress addresses. Never disable certificate verification.

```sh
task test:cluster -- preflight --kubeconfig /path/to/cluster.yaml --namespace qual-api --prometheus-url http://127.0.0.1:29090
task test:cluster -- seed --kubeconfig /path/to/cluster.yaml --namespace qual-api --prometheus-url http://127.0.0.1:29090 --fixture .local/qual-api.json --tracks 16 --events 1000 --payload-bytes 64 --webhook-url fixture
task test:cluster -- run --kubeconfig /path/to/cluster.yaml --namespace qual-api --prometheus-url http://127.0.0.1:29090 --fixture .local/qual-api.json --workload mixed --batch 100 --rate 20 --duration 5m --output reports/qualification/baseline --tier dedicated
task test:cluster -- matrix --kubeconfig /path/to/cluster.yaml --namespace qual-api --prometheus-url http://127.0.0.1:29090 --fixture .local/qual-api.json --workload mixed --batch 100 --rate 20 --output reports/qualification/scaling --tier dedicated
```

The driver uses the instance's OAuth client credentials without printing or saving them. The pinned k6 container uses constant arrival rates and bounded VUs; it records dropped iterations instead of allowing slow responses to reduce the offered request rate. Run the generator outside the API/database nodes and check its capacity. `--vus` and `--max-vus` control generator concurrency. TLS and OAuth are required for every workload.

`matrix` runs fixed API replicas 1, 2, 4 and 8 before HPA. Each configuration gets three repetitions at the selected duration, followed by a 30-minute HPA soak. Each repetition starts from the same seeded dataset: the driver checks exclusive fixture ownership, drains background jobs, truncates event/job tables in the marked disposable database, and reseeds through the API. A database containing other tracks is refused. This prevents insertion growth from biasing later replica comparisons. It restores the original replica/autoscaling configuration even after a failed run. Use short `--duration` and `--soak-duration` overrides only for functional smoke checks. Begin at a low arrival rate, then repeat at increasing rates to find the latency/error/drop inflection. Keep the database, worker and payload settings constant during the scaling comparison.

Workloads are `read-page`, `read-range`, `insert`, `update`, `mixed`, `hot` and `no-overlap`. Mixed traffic is 75% reads and 25% writes, split between new inserts and updates. Hot writes target one shared track. No-overlap updates exercise the overlap-restricted path on disjoint seeded events. Use batches of 1, 100 and 1000, and payloads such as 64, 512 and 4096 bytes where the complete request fits 1 MiB. Updates require at least as many seeded events per track as the batch size. Seeds stream bounded batches through the API; the generator never holds the full database in memory.

Create separate disposable datasets that fit within and exceed database memory. Increase `--events`, `--tracks` and payload size, then compare `database-seeded.json`'s actual database size with DB memory limits. Record whether caches were cold or warm. Use a fresh fixture path and target for each comparison so accumulated inserts do not skew later runs.

After the API scaling comparison, change one factor at a time through native instance fields: DB CPU/memory and PostgreSQL memory settings, Pooler `serverConnections`, API pool size, or worker count. Keep the connection reserve valid, wait for the observed generation and completed rollout, and repeat the same workload/rate/dataset. Compare query calls/time, cache misses, temporary writes, WAL, I/O timings, replication lag and lock waits. High hot-track contention cannot be fixed by adding API replicas.

Reports contain k6 request/event/byte rates, latency percentiles, HTTP and per-event errors, dropped arrivals, Prometheus time series, SQL samples every 15 seconds, before/after Kubernetes configuration, actual pod image IDs, k6 digest, storage class, node architecture/capacity, source revision/hash and driver architecture. Database/query samples include query IDs rather than SQL text. Dedicated-tier reports are measurements for review, not an automatic performance certification. Native ARM64 remains a supported target; qualify it separately and retain architecture in comparisons.

## Failure and recovery

Run faults only against the disposable target after seeding. Each scenario writes unique event IDs before, during and after the disruption, flushes a client ledger, and reads the events back. Acknowledged writes must remain present. Responses lost around a commit are recorded as ambiguous and checked separately; writes are never replayed automatically.

```sh
task test:cluster -- fault --kubeconfig /path/to/cluster.yaml --namespace qual-api --prometheus-url http://127.0.0.1:29090 --fixture .local/qual-api.json --fault primary-delete --output reports/qualification/primary-failure
```

Available scenarios are `api-delete`, `pooler-delete`, `switchover`, `primary-delete` and `standby-loss`. Standby loss fences all current standbys temporarily, leaving required durability enabled, then removes the fence in cleanup. Kubernetes pod deletion simulates process loss; it does not replace dedicated-node power, network partition, volume failure and storage durability exercises. Inspect the ledger, recovery interval, ambiguous results, worker queue drain and webhook delivery before accepting a release.

Keep one PostgreSQL writer and the existing compiled-schema/JWKS caches while collecting evidence. Revisit sharding only after identifying a primary write-capacity limit and a viable data-ownership boundary. Add mutable response caching or replica reads only with explicit invalidation and consistency requirements.
