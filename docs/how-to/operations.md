# Instance operations

## Scale and pause

Change `spec.api.replicaCount` and `spec.worker.replicaCount` independently. Zero stops a component. A zero-worker installation continues to enqueue notifications and deferred deletions; watch queue depth and oldest-job age. Use the CR field rather than scaling its generated Deployment, which the controller will reconcile back.

```sh
kubectl -n tads patch tadoss archive --type=merge -p '{"spec":{"worker":{"replicaCount":0}}}'
kubectl -n tads patch tadoss archive --type=merge -p '{"spec":{"paused":true}}'
```

`paused` stops ordinary reconciliation; it leaves running workloads and CNPG active. Deletion protection and finalizer handling still apply. Set `paused: false` to reconcile again. On recovery exercises, set worker replicas to zero before applying, then start workers after reviewing restored subscriptions and jobs.

CNPG `instances` is independent of API/worker replicas. Multi-server defaults need sufficient nodes and storage. Profiles add topology preferences and disruption budgets; a single-node deployment cannot provide node failure tolerance. Explicit `minAvailable` overrides the default budget.

## Retry a failed schema Job

Inspect `status.schemaJob` and its logs, fix the reported connection, schema or resource problem, then change `spec.schemaRetryNonce` to a new value. The operator creates a new immutable Job and waits for it before updating workloads. Database credentials or CA object revisions also create a new Job. Migration is idempotent for the selected schema target.

## Instance boundaries

Each instance owns its API, worker, migration Job and managed database in its namespace. Use separate namespaces, credentials and databases for separate trust domains. A shared external database shares the data visible through its connections; two instances pointed at the same schema do not isolate tenants. Platform controllers and the TADOSS operator are cluster infrastructure.

CR authors are trusted workload administrators: native env, volumes, security context and Secret references let them configure pods in the instance namespace. Do not grant Tadoss write access as an end-user API permission. Use Kubernetes RBAC and namespace policy for administrative separation. The API authorization model does not turn one shared database into a general tenant boundary.

The `tadoss.livewyer.io/v1alpha1` CRD is pre-release. Field names and behavior may change before stability; no old-version migration compatibility is promised. Review generated CRD changes with each release.

## Network policy

When enabled, the default API ingress permits TCP 8000 from any source; worker ingress denies all, including metrics on 8001. Default egress permits DNS on TCP/UDP 53, PostgreSQL on TCP 5432 and HTTP(S) on TCP 80/443 to any destination. Custom database, JWKS or webhook ports require explicit rules. Enforcement depends on the cluster CNI.

Supplying a component's ingress or egress list replaces that component's defaults. Keep DNS and database access in an explicit egress list. This example scopes API ingress to Traefik and worker metrics to monitoring, and permits a worker webhook on port 9443 in `integrations`. Adapt namespace and pod labels to the actual installation:

```yaml
networkPolicy:
  enabled: true
  api:
    ingress:
    - from:
      - namespaceSelector:
          matchLabels: {kubernetes.io/metadata.name: traefik}
        podSelector:
          matchLabels: {app.kubernetes.io/name: traefik}
      ports: [{protocol: TCP, port: 8000}]
  worker:
    ingress:
    - from:
      - namespaceSelector:
          matchLabels: {kubernetes.io/metadata.name: monitoring}
        podSelector:
          matchLabels: {app.kubernetes.io/name: prometheus}
      ports: [{protocol: TCP, port: 8001}]
    egress:
    - to:
      - namespaceSelector:
          matchLabels: {kubernetes.io/metadata.name: kube-system}
        podSelector:
          matchLabels: {k8s-app: kube-dns}
      ports: [{protocol: UDP, port: 53}, {protocol: TCP, port: 53}]
    - to:
      - podSelector:
          matchLabels: {cnpg.io/cluster: archive-db}
      ports: [{protocol: TCP, port: 5432}]
    - to:
      - namespaceSelector:
          matchLabels: {kubernetes.io/metadata.name: integrations}
        podSelector:
          matchLabels: {app: receiver}
      ports: [{protocol: TCP, port: 9443}]
```

For this private receiver, also configure `TADOSS_WEBHOOK_ALLOWED_HOSTS` with its exact URL hostname on API and worker. Network policy permission does not bypass destination validation. Add destination-specific egress on the API for a JWKS endpoint on a custom port, or on both components for an external PostgreSQL port. Kubernetes NetworkPolicy cannot select destinations by DNS name; use maintained IP ranges or a CNI feature when needed.
