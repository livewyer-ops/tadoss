# 0003. Deletion protection yields to namespace deletion

Status: accepted, 2026-09-15.

## Context

The validating admission policy denied every DELETE of a protected `Tadoss`, including the
namespace controller's. The finalizer requeued every five seconds for as long as
`spec.deletionProtection` was true. Deleting a namespace that held a protected instance hung in
`Terminating` with no documented way out.

## Decision

The admission policy exempts requests from the namespace controller and requests against a
namespace that is already terminating. The chart can disable the policy, scope it with a namespace
selector, or exempt named users.

The finalizer releases the instance when its namespace is terminating, whatever the values of
`deletionProtection` and `deletionPolicy`. Outside namespace deletion, `Retain` still removes only
the operator's owner reference from the PostgreSQL cluster.

## Consequences

`kubectl delete namespace` completes. Protection still blocks a direct delete of the instance, and
`helm uninstall` or `helmfile destroy` of the instance release, while it is enabled.

Namespace deletion removes a retained PostgreSQL cluster and its storage together with everything
else in the namespace. Move the cluster first if it must survive.
