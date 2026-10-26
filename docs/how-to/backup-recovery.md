# Backup and recovery

A retained volume is not a backup. Configure CNPG object storage and verify recovery before relying on the installation. External database owners must provide their own backup and restore procedures.

The pinned CNPG 1.29.1 platform supports native Barman backups. Backup/restore instances default to the PostgreSQL `system` image, which includes Barman; a custom image must supply those tools. This integration must be replaced with a backup plugin before upgrading to a CNPG release that removes native Barman. See the [CNPG backup reference](https://cloudnative-pg.io/docs/devel/appendixes/backup_barmanobjectstore/) and [image variants](https://github.com/cloudnative-pg/postgres-containers).

## Managed database backups

Set `spec.backends.db.cnpg.backup.enabled: true`, a six-field CNPG cron `schedule`, `retentionPolicy`, and native `objectStore` configuration. For example:

```yaml
backup:
  enabled: true
  schedule: "0 0 2 * * *"
  retentionPolicy: "30d"
  objectStore:
    destinationPath: s3://tadoss-backups/production
    s3Credentials:
      accessKeyId:
        name: backup-credentials
        key: ACCESS_KEY_ID
      secretAccessKey:
        name: backup-credentials
        key: ACCESS_SECRET_KEY
    wal:
      compression: gzip
    data:
      compression: gzip
```

Create the credentials Secret separately in the instance namespace. Confirm the generated ScheduledBackup and CNPG Cluster status show a successful backup and WAL archival. `BackupPolicyReady` reflects CNPG's reported success; it does not certify a recovery exercise.

## Recover into a new instance

Create a new instance name with a separate database and the operator/runtime builds matching the backup's schema history. A version label alone is insufficient for development builds. Check the [changelog](../../CHANGELOG.md) for migration prerequisites before selecting an image. Set `backends.db.cnpg.restore.enabled: true`, `source` to the original CNPG server name (normally `<old-instance>-db`), and `objectStore` to the source archive. Supply `targetTime` for a point-in-time recovery, or omit it to recover to the latest available point. Keep the original instance and backups intact.

```yaml
restore:
  enabled: true
  source: original-db
  targetTime: "2026-10-20 12:00:00+00"
  objectStore:
    destinationPath: s3://tadoss-backups/production
    s3Credentials:
      accessKeyId: {name: backup-credentials, key: ACCESS_KEY_ID}
      secretAccessKey: {name: backup-credentials, key: ACCESS_SECRET_KEY}
```

Apply, wait for CNPG recovery and the schema Job, then check API and worker readiness. Verify known schemas, tracks, payloads and event ranges through the API. Test writes and deferred deletion on disposable restored records. Record backup identity, target time, recovered records and elapsed recovery time. Only then switch clients or ingress to the recovered instance. Bootstrap/restore settings cannot be changed on an existing database.

Recovery restores webhook subscriptions and outstanding jobs too. Keep the restored worker at zero replicas and restrict outbound traffic until you have reviewed subscriptions and jobs; otherwise notifications may be delivered again. Configure `worker.replicaCount: 0` before first apply for this review.

For external PostgreSQL, create a new URI Secret for the recovered database. Set `backends.db.external.caConfigMap` to a ConfigMap containing the CA and its key; the runtime forces certificate and hostname verification. Test a wrong CA or hostname before accepting the installation.

## Reuse a retained database

With deletion protection disabled and `deletionPolicy: Retain`, deleting the Tadoss resource detaches both its CNPG Cluster and ScheduledBackup. CNPG keeps the database, CA/application Secrets, WAL archival and scheduled base backups running. Preserve object-store credentials and the namespace. The administrator owns these retained resources; namespace deletion removes them regardless of Retain.

A new managed instance does not adopt an existing Cluster, including one with the same name. Connect a new instance through the external provider using builds compatible with the retained database's schema history. The same [migration prerequisites](../../CHANGELOG.md) apply to retained databases and restored backups. In the original namespace, copy the public CA to a ConfigMap and refer to the retained application URI Secret:

```sh
kubectl -n tads get secret archive-db-ca -o jsonpath='{.data.ca\.crt}' | base64 -d > retained-ca.pem
kubectl -n tads create configmap retained-db-ca --from-file=ca.pem=retained-ca.pem
```

```yaml
apiVersion: tadoss.livewyer.io/v1alpha1
kind: Tadoss
metadata: {name: reattached, namespace: tads}
spec:
  version: 0.1.0-rc2
  profile: single-server
  worker: {replicaCount: 0}
  backends:
    db:
      providedBy: external
      external:
        existingSecret: archive-db-app
        caConfigMap: {name: retained-db-ca, key: ca.pem}
```

Verify data through the new API before enabling its worker or redirecting clients. Its API token is new unless you supply an existing token Secret. Keep the retained Cluster and backup schedule under separate administration. If CNPG rotates its CA, update the external CA ConfigMap too; the new instance watches that ConfigMap. Copy credentials through your secret manager when moving namespaces, and use a URI hostname covered by the database certificate.
