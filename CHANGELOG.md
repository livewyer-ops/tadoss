# Changelog

## Unreleased

- Reject API database operations while the schema is incompatible or uninitialised. Clarify database reset and recovery requirements before the first public release.
- Reuse TAMOSS operator implementations for managed apply, namespace scope, event filtering, CNPG rollouts and condition handling; index Secret and ConfigMap references for watch dispatch.
- Bound API database admission and reduce event-batch, metadata-update and webhook fanout work; use existing indexes for collection filters.
- Consolidate the pre-release schema into `0001_baseline`, including all event, collection and job-retention indexes. Existing development databases using the previous migration history are incompatible: recreate disposable databases, or export required data with the previous build and import it into a fresh database. Do not relabel the old migration ledger or restore an old physical backup into the new build. Failed baseline creation rolls back and can be retried; recovery to an older build requires its matching database backup.
- Separate runtime, operator, platform and instance configuration; use explicit release and schema selection.
- Add JWT verification, webhook delivery and leased asynchronous deletion for the pinned TADS contract.
- Correct read-only enforcement, JSON equality, invalid-event isolation, SQL pagination and rollout readiness.
- Add native workload configuration, external database TLS, CNPG backup/new-instance restore, operational conditions and restricted support bundles.
- Add environment tasks, current-contract checks, profile qualification and immutable release artifacts.
