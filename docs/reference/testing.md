# Testing and qualification

| Command | Evidence |
| --- | --- |
| `task test:fast` | Settings, environment command semantics, vendor bundle and support redaction |
| `task test:all` | PostgreSQL API, authentication, webhook and deletion integration tests |
| `task test:tads` | Pinned contract/handler inventory plus semantic tests |
| `task operator:test` | Resource tests and real API-server envtest with the race detector |
| `task test:manifests` | Every environment render, issuer chart lint, contract and documentation checks |
| `task kind:test PROFILE=... CLUSTER=...` | Real migration, verified HTTPS ingress, API, worker, strict diff and deployed behaviour |
| `task security:audit` | Locked Python and Go dependency advisories |

CI qualifies local-kind, edge, single-server and multi-server and a native ARM64 runner. Release qualification uses the built immutable image references. Chainsaw covers lifecycle, recovery, deletion/retention, namespace deletion, external databases and credential rotation. Each test creates its own resources.

`reports/tads-conformance.json` is an operation inventory, not certification of every possible input. Semantic regression tests cover read-only state, JSON equality, invalid batch records, keyset pagination, range boundaries, JWT claims/scopes, webhook ownership/delivery, stale worker claims and deletion cutoffs. Backup success must additionally be followed by the recovery exercise in the operations guide.

Worker regressions exercise consecutive deletion failures, malformed HTTP, slow headers/bodies, blocked sends and TLS handshakes, revision fencing and credential rotation. Operator regressions cover CA mounts, metadata fingerprints, admission and Retain ownership. The backup scenario requires a new scheduled backup after the parent instance has been deleted.

Renovate custom managers track the Go PostgreSQL image, literal Chainsaw images and release metadata chart versions. Helmfile and release chart updates share one Helm dependency group. Check extraction with `renovate --platform=local --dry-run=extract` as described in the [Renovate local platform guide](https://docs.renovatebot.com/modules/platform/local/); retain `task release:check` to catch version drift.
