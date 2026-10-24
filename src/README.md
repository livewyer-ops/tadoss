# Runtime

`app/tadoss/api` translates HTTP requests into application operations. `application/background.py` owns webhook subscriptions, the transactional outbox and deletion jobs. `domain` validates the pinned contract. `adapters` contains PostgreSQL storage and outbound HTTP. `db` contains ordered migrations; `auth.py` and `settings.py` own authentication and process configuration.

The API and worker use the same image and database. Run `tadoss migrate` explicitly before either process. `/healthz` reports process health; `/readyz` checks database connectivity and the exact migration set. API requests never apply migrations.

```sh
uv sync --project src --frozen
TADOSS_DEV=true uv run --project src tadoss migrate
TADOSS_DEV=true uv run --project src tadoss serve
TADOSS_DEV=true uv run --project src tadoss worker
```

`vendor/bbc-tads` is a pinned Git submodule. `scripts/vendor_spec.py` produces the bundled contract; `scripts/check_contract.py` detects drift. See the repository documentation for configuration and deployment.
