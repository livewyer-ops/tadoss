#!/usr/bin/env bash
set -euo pipefail
umask 077
kubeconfig=$1
namespace=$2
instance=$3
hostname=$4
kubectl --kubeconfig "$kubeconfig" get namespace "$namespace" >/dev/null
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT

existing=$(kubectl --kubeconfig "$kubeconfig" -n "$namespace" get secret "$instance-authentik-config" --ignore-not-found -o name)
if [ -z "$existing" ]; then
  openssl rand -hex 48 | tr -d '\n' > "$temporary/key"
  openssl rand -hex 32 | tr -d '\n' > "$temporary/password"
  kubectl --kubeconfig "$kubeconfig" -n "$namespace" create secret generic "$instance-authentik-config" \
    --from-file=AUTHENTIK_SECRET_KEY="$temporary/key" \
    --from-file=AUTHENTIK_BOOTSTRAP_PASSWORD="$temporary/password" \
    --from-literal=AUTHENTIK_POSTGRESQL__HOST="$instance-authentik-db-rw.$namespace.svc.cluster.local" \
    --from-literal=AUTHENTIK_POSTGRESQL__NAME=authentik \
    --from-literal=AUTHENTIK_POSTGRESQL__USER=authentik
fi
existing=$(kubectl --kubeconfig "$kubeconfig" -n "$namespace" get secret "$instance-oauth-client" --ignore-not-found -o name)
if [ -z "$existing" ]; then
  openssl rand -hex 32 | tr -d '\n' > "$temporary/client-secret"
  kubectl --kubeconfig "$kubeconfig" -n "$namespace" create secret generic "$instance-oauth-client" \
    --from-file=client_secret="$temporary/client-secret" \
    --from-literal=client_id="$instance-api" \
    --from-literal=token_endpoint="https://$hostname/application/o/token/" \
    --from-literal=scopes='tads-api/read tads-api/write tads-api/delete'
fi
