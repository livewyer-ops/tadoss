# 0004. Separate platform, operator and instance deployment

Status: accepted, 2026-10-24. Supersedes the operator-chart and instance composition in [0001](0001-helmfile-orchestrates-deployment.md).

## Context

The platform prerequisites are shared infrastructure. Operator installation defaults and individual instances have different owners and lifecycles. One Helm deployment for all of them makes those boundaries hard to review.

## Decision

Helmfile installs CNPG, Traefik, cert-manager and the small issuer chart. Native Kustomize manifests install the operator, CRD, RBAC and deletion policy. Environment directories compose a platform values file, an operator overlay and independent instance overlays. Task wraps these commands for convenience.

Installation defaults use immutable, content-named ConfigMaps. Explicit instance fields win over installation defaults; profiles supply workload sizes. The controller resolves a working copy and leaves the stored CR intent intact. Release installations pin the operator digest; its catalogue binds the runtime digest and schema target.

## Consequences

Operators can use existing prerequisites and deploy multiple instances independently. Defaults changes roll out the operator. Old defaults ConfigMaps need explicit cleanup after their Deployment, ReplicaSet and Pod references are gone. Admission and finalizer behavior from [0003](0003-deletion-protection-yields-to-namespace-deletion.md) remains; its policy is now packaged in the native installation.
