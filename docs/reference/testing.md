# Testing and qualification

| Command | Evidence |
| --- | --- |
| `task test:fast` | Authentication, settings, contract, native command semantics, repository checks and support redaction |
| `task test:all` | PostgreSQL API, authentication, webhook and deletion integration tests, with line and branch coverage |
| `task test:tads:contract` | Pinned document, registered operations, response schemas and headers without PostgreSQL |
| `task test:tads:semantics` | PostgreSQL record, event, webhook and deletion behaviour |
| `task test:tads` | Both contract and semantic suites, with separate JUnit reports |
| `task operator:test` | Resource tests and real API-server envtest with the race detector and Go coverage |
| `task test:manifests` | Every environment render, issuer chart lint, Chainsaw syntax and documentation checks |
| `task kind:test PROFILE=... CLUSTER=...` | Real migration, verified HTTPS ingress, API, worker, strict diff and deployed behaviour |
| `task security:audit` | Locked Python and Go dependency advisories |

CI qualifies local-kind, single-server and multi-server, including single-server on a native ARM64 runner. Release qualification uses the built immutable image references. Chainsaw covers lifecycle, recovery, deletion/retention, namespace deletion, external databases and credential rotation. Each test creates its own resources.

Security policy and infrastructure regressions remain in `task test:all`; authentication also runs in `task test:fast`. `task test:tads` writes `reports/tads-contract.xml` and `reports/tads-semantics.xml`, both retained by CI.

The pytest contract cases identify every published operation in JUnit output and compare it with the native FastAPI routes. Shared API fixtures validate responses and documented headers directly against the unmodified vendored BBC specification and its external schemas. HEAD responses must retain paging headers and contain no body. Semantic regression tests cover read-only state, JSON equality, invalid batch records, keyset pagination, range boundaries, JWT claims/scopes, webhook ownership/delivery, stale worker claims, deletion cutoffs and rollback when commit fails. These checks do not certify every possible input. Backup success must additionally be followed by the recovery exercise in the operations guide.

`tests/api`, `tests/adapters` and `tests/workers` group the database-backed suites by behaviour. `tests/conftest.py` creates and removes an isolated PostgreSQL schema per test; set `TADOSS_TEST_DATABASE` to use another test server. For a focused run, use `uv run --project src --frozen pytest -c src/pyproject.toml -q tests/api/test_events.py`. `task test:all` writes JUnit to `reports/python.xml` and coverage to `reports/python-coverage.xml`, and prints a per-module coverage report.

`task operator:test` writes the native Go coverage profile to `reports/go-coverage.out` and a function summary to `reports/go-coverage.txt`. Open it with `go tool cover -html=../reports/go-coverage.out` from `operator`. CI retains Python and Go reports in the `test-reports` artifact.

Worker regressions exercise consecutive deletion failures, malformed HTTP, slow headers, unfinished response bodies, blocked sends and TLS handshakes, revision fencing and credential rotation. Operator regressions cover CA mounts, metadata fingerprints, admission and Retain ownership. The backup scenario requires a new scheduled backup after the parent instance has been deleted.

Renovate custom managers track the Go PostgreSQL image, literal Chainsaw images and release metadata chart versions. Helmfile and release chart updates share one Helm dependency group. Check extraction with `renovate --platform=local --dry-run=extract` as described in the [Renovate local platform guide](https://docs.renovatebot.com/modules/platform/local/); retain `task release:check` to catch version drift.

Kind builds use one per-invocation tag shared by build, load and test. Set `IMAGE_TAG` to choose the tag explicitly. `reports/<profile>/images.json` records local Docker image identities. Supply both `RUNTIME_IMAGE` and `OPERATOR_IMAGE` to reuse existing builds across profiles. Locally available images are loaded into Kind; other references are pulled from their registry. Released digest references exercise the embedded catalogue. Kind preparation preserves existing platform values and unrelated workload settings. Port changes require a new cluster.

Chainsaw owns deployed resource assertions, database-check Jobs, cleanup and JUnit reports. The Job tests database TLS and certificate hostname rejection. The same qualified runtime image then runs the API checks from the host network through verified HTTPS ingress: authentication, mixed-success batches, forward and reverse pagination, GET/HEAD headers and deferred deletion. The test container receives the ingress CA and a hostname mapping for the selected Kind host port. Curl checks readiness before that workflow. `curl` is required alongside the Aqua-managed tools.

Chainsaw uses native assertions and bindings for Kubernetes polling, certificate copying and backup selection. HTTP fixtures live in `operator/test/chainsaw/fixtures/api_check.py` and `tests/deployed/checks.py`; the tests load them into ConfigMaps for Jobs. Keep protocol logic in those maintained sources so `task lint:all` checks it. Repository tests reject inline scripts, shell entrypoints, inline Python and missing fixture files, including cleanup operations. Documented commands are checked against Task's native JSON listing.
