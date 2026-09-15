# 0001. Helmfile orchestrates the whole deployment

Status: accepted, 2026-09-15.

## Context

`scripts/deploy.py` drove helmfile, kubectl, helm and kustomize in sequence. It hard-coded the
instance namespace and both image repositories, generated YAML by string replacement, and needed a
host Python that the aqua toolchain does not install. Four instance overlays and four environment
overlays existed to set one enum field, and twelve of the twenty files were identical.

## Decision

One helmfile, `deploy/helmfile.yaml.gotmpl`, owns the platform charts, the TLS issuer, the operator
chart and the Tadoss instance as a single dependency graph. Environments are files under
`deploy/environments`; each merges over `deploy/values/defaults.yaml`. The instance is the kustomize
base under `deploy/instances/base`, consumed through helmfile with one JSON patch built from values.

Disabled platform components are omitted from the rendered state with template guards. Helmfile's
`installed: false` is not used, because it uninstalls a release of the same name that another team may
own on a shared cluster.

Task runs helmfile and kubectl directly. The deployment path has no Python.

## Consequences

The kustomize binary and the helm-diff plugin become dependencies; aqua installs the first and
`task setup` installs the second into a repository-local plugin directory.

The CRD ships inside the operator chart's templates with `helm.sh/resource-policy: keep`, so
`helm upgrade` upgrades it and `helm uninstall` retains it. The separate CRD apply step is gone.

Users who apply the kustomize base directly create the namespace first.
