# ADR-003: OIDC authentication design

## Status

Accepted.

## Context

Humans and services need corporate identity without receiving inference-provider keys. LiteLLM's native JWT authentication and custom-auth-to-virtual-key integration are Enterprise features, while LiteLLM OSS exposes a custom-auth hook that can return `UserAPIKeyAuth` directly.

## Decision

Humans authenticate with Authorization Code and PKCE through the OpenCode plugin; services use workload identity or client credentials. The LiteLLM OSS custom-auth module validates short-lived JWT signatures, issuer, audience, allowed algorithms, time claims, and configured scopes using cached OIDC discovery and JWKS data. It maps issuer-scoped subjects and trusted claims to one deterministic, pre-provisioned policy identity without minting LiteLLM virtual keys. The optional Keycloak Compose profile is development-only; production uses an external standards-compliant IdP.

## Consequences

- The demonstrated path requires no LiteLLM Enterprise authentication feature or per-request token introspection.
- The project owns JWKS rotation, JWT validation, claim mapping, and their security tests.
- Production IdP registration, claims, MFA, lifecycle, and availability remain organizational responsibilities.
- Custom auth preserves an explicit administrative route allowlist for bootstrap
  and the shipped accounting audit; the master credential is denied everywhere
  else and replaced with a non-secret downstream identifier.
