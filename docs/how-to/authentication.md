# Authentication and secret rotation

Use OAuth2 client credentials for a shared deployment. Bearer tokens are signed JWTs verified against the configured issuer, audience and JWKS URI. Development installations retain the generated query token by default; that opaque token authenticates through `?access_token=...`.

## Configure OAuth2

An existing issuer can be configured under `spec.auth.oauth2`. To install an independent Authentik issuer, first install the environment's CloudNativePG and Traefik prerequisites. Create a trusted TLS Secret in the instance namespace and route the authentication hostname to that ingress, then run:

```sh
task auth:apply KUBECONFIG=/path/to/cluster.yaml INSTANCE=tadoss AUTH_NAMESPACE=tads AUTH_HOST=auth.tadoss.example.com AUTH_TLS_SECRET=tadoss-public-tls
```

This uses the pinned Authentik Helm chart and declarative blueprints. It creates a separate PostgreSQL database with a 5 GiB PVC, one server and one worker. Bootstrap creates `<instance>-authentik-config` and `<instance>-oauth-client` only when absent; repeated applies preserve their values. The Authentik administrator is `akadmin`; its initial password is in `<instance>-authentik-config` under `AUTHENTIK_BOOTSTRAP_PASSWORD`. Keep that Secret restricted and change the administrator password after first login. Changing the bootstrap value does not change an existing administrator's password.

Blueprint application is asynchronous. Before enabling OAuth2, confirm that `https://auth.tadoss.example.com/application/o/tadoss/.well-known/openid-configuration` returns discovery metadata and that the client can obtain a token. For another instance name, replace `tadoss` in the discovery, issuer and JWKS paths, and in the audience and Secret name:

```yaml
spec:
  auth:
    oauth2:
      issuer: https://auth.tadoss.example.com/application/o/tadoss/
      audience: tadoss-api
      jwksURI: https://auth.tadoss.example.com/application/o/tadoss/jwks/
      clientCredentialsSecret: tadoss-oauth-client
```

Apply the instance and verify a bearer-authenticated API request. Then set `spec.auth.tokenEnabled: false` and apply again to disable query authentication. The operator retains the old token Secret for rollback, clears `status.tokenSecret`, and removes the token from API pods. Disabling query tokens requires OAuth2 or Basic authentication. Existing installations that omit `tokenEnabled` keep query authentication enabled.

For an external issuer, create the referenced client-credentials Secret in the same namespace with UTF-8 keys `client_id`, `client_secret`, `token_endpoint` and `scopes` (space-separated). The credential task uses POST-body client authentication. This Secret is read by access tooling; the operator never mounts it into API or worker pods. TADOSS validates bearer JWTs without needing the client secret.

## Use TADS Checker

Load the live endpoint and credentials:

```sh
credentials=$(task env:credentials ENV=production INSTANCE=tadoss KUBECONFIG=/path/to/cluster.yaml) && eval "$credentials"
unset credentials
```

`INSTANCE` is the rendered Kubernetes resource name. `AUTH_METHOD=auto` selects OAuth when an issuer is configured, otherwise query authentication. It fails if the selected credentials are unavailable. `AUTH_METHOD=oauth` or `AUTH_METHOD=query` selects explicitly; disabled query authentication cannot be exported. Exports are shell-quoted and clear credentials left over from the other method. Keep their output out of logs and committed files.

In [TADS Checker](https://check.tads.lwy.sh/), use:

| Checker field | OAuth2 value |
| --- | --- |
| TADS API URL | `TADOSS_URL` |
| Authentication | OAuth client credentials |
| Token endpoint | `TADOSS_OAUTH_TOKEN_URL` |
| Client ID | `TADOSS_OAUTH_CLIENT_ID` |
| Client secret | `TADOSS_OAUTH_CLIENT_SECRET` |
| Client authentication | POST body (`post`) |
| Scopes | `TADOSS_OAUTH_SCOPES` |
| Specification | Auto detect |

Enter variable values without shell quotes. For a development query token, select **URL access token** and use `TADOSS_API_TOKEN`. The **Bearer token** option needs an already-issued JWT. Keep credentials out of the base URL. The hosted checker needs public HTTPS with a trusted certificate and creates and deletes its own test resources.

`task kind:credentials CLUSTER=<name>` provides the same exports for a Kind environment. For a local host port, supply `API_URL=https://<ingress-host>:<port>` and configure your client to trust the cluster's certificate; the Kubernetes ingress endpoint does not include Kind's host port.

## Rotation and verification

OAuth scope defaults are `tads-api/read`, `tads-api/write`, `tads-api/delete`, and `tads-api/admin`. The supplied Authentik client receives read, write and delete scopes and tokens valid for five minutes. The CRD requires issuer, audience and JWKS URI together, HTTPS URLs, and supported asymmetric algorithms (default `RS256`). The controller rejects embedded URL credentials. The standalone runtime permits HTTP only in development mode. JWT verification fails closed when keys or claims cannot be verified. The verifier caches JWKS for five minutes and limits unknown-key refreshes to once per 30 seconds. Publish new signing keys before issuing tokens that use them; known cached keys remain usable during a brief JWKS outage.

For the supplied Authentik deployment, rotate `client_secret` in `<instance>-oauth-client`, restart its server and worker Deployments to reload their environment, and wait for the blueprint to update the provider. Verify the old secret is rejected and the new one obtains a token. Already-issued tokens remain valid until expiry. A hostname change also requires updating the Secret's `token_endpoint`, TLS and ingress, and the instance's issuer/JWKS configuration. Bootstrap never overwrites existing Secrets. Back up the Authentik database and bootstrap Secret together; they have an independent lifecycle from the Tadoss resource.

For query authentication, configure `spec.auth.tokenSecret` with a Secret containing `token`, or let the operator generate one. `spec.auth.basicSecret` refers to a Secret with `username` and `password`. Supply credentials through your secret-management system. Use HTTPS and omit query strings from access logs when using query authentication.

Update a referenced API or database Secret and wait for `task env:wait`. The operator fingerprints object identity and resourceVersion, never credential bytes. Object metadata edits also cause rollouts; database and CA revisions rerun the idempotent schema Job. During rollout different pods can accept different credentials; verify rejection of the old credential after all replicas update. Workers and schema Jobs do not receive API authentication credentials. Never put credentials in a CR, issue, test report or support bundle.

Webhook subscriptions belong to the authenticated subject; administrators may manage all subscriptions. Delivery secrets are write-only and preserved when omitted from updates. URL or filter changes invalidate queued deliveries. Tags, authentication header names, delivery secrets and reasserting an active status preserve the backlog. Disabling and re-enabling increments the revision so old jobs cannot revive. Filter array order does not matter; an absent filter differs from an empty list. GET responses, including server-owned error details, can be PUT back with `status: created` to re-enable a subscription; the server discards the old error. If credentials rotate during a failed send, that job retries the current credentials with a fresh attempt budget. Delivery is at least once: consumers must tolerate retries. Redirects and private, loopback, link-local and metadata destinations are denied unless an explicit host exception permits a private destination; metadata endpoints remain denied. DNS addresses are checked at delivery and the connection is pinned to the checked address.

Webhook retries use `TADOSS_WEBHOOK_MAX_ATTEMPTS`. Expected transport errors, including malformed HTTP, and unexpected sender failures update the fenced job and subscription together. Logs contain exception classes, job identifiers and SQLSTATE where available, without receiver bytes, exception messages or credentials. See [worker limits](../reference/configuration.md#database-tls-and-worker-limits) for deadline and DNS behavior.
