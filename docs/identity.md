# Identity integration

## Token validation

The gateway uses OIDC discovery and cached JWKS. It validates asymmetric
signature, allowed algorithm, exact issuer, at least one configured audience,
`exp`, `nbf` when present, and configured scopes. An unknown `kid` causes one
immediate JWKS refresh for key rotation, with subsequent unknown-key refreshes
rate-limited to avoid turning forged tokens into IdP traffic. There is no
per-request introspection. Invalid tokens return 401; discovery/JWKS failures
return 503 so clients retry instead of needlessly restarting login.

Required production inputs:

```text
OIDC_ISSUER_URL
OIDC_AUDIENCE
OIDC_GROUP_CLAIM
OIDC_ROLE_CLAIM
```

Optional inputs include `OIDC_REQUIRED_SCOPES`, `OIDC_ALLOWED_CLIENT_IDS`,
`OIDC_ALLOWED_ALGORITHMS`, cache TTL, and clock skew. Do not allow symmetric
algorithms: they would make a client secret a token-signing secret.
`OIDC_CLIENT_ID` is accepted as the single-client fallback when an explicit
allowed-client list is not configured.

## Human login

The OpenCode plugin discovers the IdP, creates a PKCE verifier/challenge and
random state, binds an ephemeral callback server to `127.0.0.1`, and returns the
authorization URL through OpenCode's OAuth hook. It never collects a password.
The IdP must allow the registered loopback redirect URI. When refresh-token
rotation is required, the production IdP configuration must allow the
`offline_access` scope for both the registered OpenCode client and the applicable
user authorization policy; requesting the scope from the client is not sufficient
if either policy denies it.

For SSH/headless environments, add a standards-compliant device-flow method to
the same plugin hook if the target IdP supports RFC 8628. Do not emulate device
flow by asking users to paste passwords.

## Services

Map service client IDs explicitly and require `kind: service`. Prefer workload/
federated OIDC where available; client credentials are the portable fallback.
The stable application identity comes from the trusted `azp`/`client_id` claim.
`X-Enterprise-AI-Client` is retained only as sanitized, untrusted telemetry.

## Mapping behavior

Mappings are ordered and first-match wins so one request has one LiteLLM team.
Specific administrator/service mappings belong before broad group mappings.
Unknown groups, missing selectors, missing required claims, and ambiguous
organization policy should fail closed.

The stable user identifier is `<issuer>|<subject>`. Logs need not store email;
email is optional convenience metadata. Never use email as the authorization key.
