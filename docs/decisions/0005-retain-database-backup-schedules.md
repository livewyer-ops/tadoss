# 0005. Retain database backup schedules

Status: accepted, 2026-10-24. Extends the Retain behavior in [0003](0003-deletion-protection-yields-to-namespace-deletion.md).

## Context

Removing a Tadoss instance with `deletionPolicy: Retain` preserved PostgreSQL but allowed Kubernetes to delete its ScheduledBackup. WAL archival continued, while scheduled base backups and their retention processing stopped.

## Decision

Before releasing the instance finalizer, remove only that instance's owner reference from both its CNPG Cluster and ScheduledBackup. Preserve unrelated owner references. A failed patch keeps the finalizer so reconciliation can retry safely. Namespace deletion still releases the finalizer without detaching resources.

## Consequences

CNPG continues scheduled backups for a retained database while its credentials, object store and namespace remain available. The administrator now owns the retained Cluster and ScheduledBackup. A replacement Tadoss instance connects through the external database provider; it does not adopt these resources. Deleting the namespace removes them.
