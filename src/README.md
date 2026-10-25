# Runtime

`app/tadoss/api/routes` contains native FastAPI routers for schemas, tracks, events and service operations. Shared dependencies authenticate and validate requests, then open a PostgreSQL transaction which commits before the response is sent. The published OpenAPI document remains the pinned TADS contract.

`application/records.py` and `application/events.py` own mutation rules and transactional notifications. `application/background.py` owns webhook subscriptions, the outbox and deletion jobs. `domain` contains contract validation and time-range rules. `adapters` contains PostgreSQL queries and outbound HTTP. `db` contains ordered migrations; `auth.py` and `settings.py` own authentication and process configuration.

The API and worker use the same image and database. Run `tadoss migrate` explicitly before either process. `/healthz` reports process health; `/readyz` checks database connectivity and the exact migration set. API requests never apply migrations.

```sh
uv sync --project src --frozen
TADOSS_DEV=true uv run --project src tadoss migrate
TADOSS_DEV=true uv run --project src tadoss serve
TADOSS_DEV=true uv run --project src tadoss worker
```

`vendor/bbc-tads` is a pinned Git submodule. `scripts/vendor_spec.py` produces the bundled contract; `tests/test_repository.py` detects drift. See the repository documentation for configuration and deployment.

Runtime interfaces are annotated. Pyright checks the package and applies strict checking to authentication, request dependencies and record/event operations. Contract documents retain arbitrary JSON values validated against the pinned schemas. Run `task lint:all` and `task test:all`; the latter reports line and branch coverage. Shared PostgreSQL and API fixtures live in `tests/conftest.py`, with suites grouped under `tests/api`, `tests/adapters` and `tests/workers`.
