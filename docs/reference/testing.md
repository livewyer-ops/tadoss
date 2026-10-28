# Testing and qualification

| Command | Evidence |
| --- | --- |
| `task test:fast` | Authentication, settings, contract, native command semantics, repository checks and support redaction |
| `task test:all` | PostgreSQL API, authentication, webhook and deletion integration tests, with line and branch coverage |
| `task test:performance` | Isolated real-HTTP load test with OAuth, latency distributions, process CPU and peak RSS |
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

## Performance qualification

For deployed multi-server tests, monitoring setup, open arrival-rate workloads and failover ledgers, use the [high-load qualification guide](../how-to/high-load.md). `task test:cluster` requires a disposable namespace and verifies HTTPS/OAuth and actual Prometheus discovery before exercising it.

Run `task test:performance -- --worker` to benchmark a separate API process against a temporary PostgreSQL schema. The harness creates a local RSA/JWKS issuer and uses bearer tokens; `--worker` also starts a worker and webhook receiver. Child processes stop before the temporary schema is removed, including on failure. `TADOSS_TEST_DATABASE` selects the test database. The harness never targets an existing API deployment.

The default dataset contains eight tracks with 1,000 events and 256-byte text payloads each. Workloads cover forward keyset pages, timerange reads, updates of 1/100/1,000 existing events, writes to a shared track, and a 75% read / 25% write mix. These are update benchmarks; fixture insertion is outside the measurements. `--payload-bytes`, `--events`, `--tracks`, `--pool-size`, `--cases` and `--concurrency` select the dataset and load. Each workload warms up, then runs three measured repetitions by default. HTTP errors, transport errors and per-event failures make the command fail while retaining its report.

```sh
task test:performance -- --duration 10 --repeats 3 --concurrency 1 10 50 100 --worker --allow-overload
task test:performance -- --duration 60 --repeats 1 --concurrency 50 --cases mixed --worker
```

Reports default to `reports/performance/report.json`; use `--output` to retain comparisons. They identify the source revision, runtime source hash, dirty working tree, package versions, architecture and machine CPU count. Results include p50/p95/p99 latency, throughput, response outcomes, API CPU time and sampled peak API RSS (Linux). CPU and memory do not include PostgreSQL, the load generator or worker. Final API metrics and queue status accompany each report. Child logs are private files and are not uploaded by CI.

`--allow-overload` explicitly accepts HTTP 503 when probing saturation; it still fails transport errors, other HTTP errors and per-event failures. Reports retain each status count and distinguish total request throughput from HTTP 200 throughput. A successful saturation run does not mean every request was served. The full manual workflow enables this option; the CI smoke test requires all requests to succeed. The load client expires idle connections before the server's keepalive timeout and never retries failed requests.

CI runs a short smoke workload to exercise the harness, not to enforce speed thresholds. The manually dispatched Performance workflow runs full workloads on native AMD64 and ARM64. Compare repeated runs on the same hardware, dataset, worker setting and runtime hash; shared runners are not suitable for hard latency gates. Run TADS contract/semantic and cancellation tests alongside any proposed optimization. Performance changes must preserve response ordering, partial batch results, commit-before-response and transactional notification delivery.

Use `--loop uvloop --http httptools` only in a test environment with those optional packages installed. Record their versions and compare against `--loop asyncio --http h11` on the same host before changing production dependencies. Use the existing OpenTelemetry PostgreSQL instrumentation for query timing and `EXPLAIN (ANALYZE, BUFFERS)` on representative test queries. Benchmark pool sizes against the total connection budget, including API/worker replicas and maintenance clients.
