# Contributing

Open an issue describing the problem or intended behaviour. For code changes, include a reproducible example and tests for meaningful behaviour. Keep generated files and examples consistent with their sources.

```sh
git submodule update --init --recursive
aqua install
task setup
task check
```

Use `task operator:generate` after CRD or RBAC changes. Test runtime changes with PostgreSQL; deployment changes also need `task kind:test` on the affected profiles. Never commit credentials, local kubeconfigs or generated support reports. Pull requests should state the problem, resulting behaviour, checks run and remaining limits.

Use `task lint:fmt` for Python and Go formatting. `task check` includes type checking, coverage reporting, race-enabled operator tests, format checks and manifest validation. See [testing and qualification](docs/reference/testing.md) for focused commands and report locations.

See [security reporting](SECURITY.md) for vulnerabilities and the [code of conduct](CODE_OF_CONDUCT.md) for participation expectations.
