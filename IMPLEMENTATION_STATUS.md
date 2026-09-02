# Implementation status

Last updated: 2026-09-02

## Current phase

Phases 0–8 are represented in the POC. Static checks, unit tests, the official
OpenCode load smoke, and the mock Compose suite pass. A disposable two-host
staging deployment has exercised a separate gateway/Keycloak/PostgreSQL host and
OpenCode client host. Live-provider and target corporate-IdP validation remain
opt-in because they require protected deployment credentials.

## Completed

- Created the generic monorepo and pinned upstream/runtime versions.
- Verified current LiteLLM, OpenCode, and OpenRouter public extension points.
- Implemented OIDC discovery/JWKS validation and configurable identity mapping.
- Implemented team/model/privacy/budget/rate policy decisions.
- Added LiteLLM custom-auth integration without virtual keys.
- Added OpenRouter catalog synchronization, filtering, aliases, and stale-cache fallback.
- Added an OpenCode plugin using native OAuth/provider hooks and PKCE.
- Added Python, TypeScript, and curl service examples.
- Added a pinned Compose deployment scaffold with optional Keycloak and mock provider.
- Added Python, TypeScript, and Docker-oriented end-to-end test definitions for
  identity, model policy, persisted spend attribution, team-budget denial,
  completions, streaming, tools, fallback, and unchanged service examples.
- Added a CI provider-switch sequence that runs the same service client before
  and after changing `general-fast` from the OpenRouter adapter to a direct one.
- Exercised browser Authorization Code + PKCE, rotated refresh tokens across a
  client restart, exact role/group/service authorization, database-backed budget
  denial, restart persistence, streaming, and a real OpenCode built-in read-tool
  round trip in isolated two-host staging.
- Exercised PostgreSQL, Keycloak, and gateway interruption/recovery, loopback-only
  listeners, external port denial, and a reverse-proxy route allowlist.
- Added deployment, security, identity, operations, client, and service docs plus ADRs.

## In progress

- Validate the live-provider and organizational-IdP paths in protected staging.

## Next

- Configure a real organizational IdP and HTTPS ingress.
- Run the opt-in OpenRouter validation with a funded server-side key.
- Run the live OpenRouter-to-direct provider-switch procedure in staging.
- Replace example alias targets and provider allowlists with approved choices.
- Connect production Prometheus/Grafana and a managed secrets backend.
- Add and burst-test coordination Redis before using multiple gateway workers or
  replicas; the validated POC deployment deliberately uses one worker.
- Run organization-specific load, backup/restore, disaster-recovery, and ingress
  certificate tests in the target production platform.

## Open questions

- Which IdP claim is the authoritative team/group claim?
- Which OpenRouter providers and data jurisdictions are approved?
- What are the production budgets and rate limits?
- Which model IDs should back the stable aliases at pilot launch?

## Decisions made

- LiteLLM remains the stable internal OpenAI-compatible API.
- OpenRouter is a replaceable backend, not a client-visible credential boundary.
- LiteLLM OSS custom auth validates OIDC JWTs directly; no virtual keys are issued.
- OpenCode 1.18.26 native `auth`, `config`, `provider.models`, and custom `fetch`
  hooks are sufficient. A loopback forwarding proxy is not required.
- The OpenCode plugin uses Authorization Code + PKCE on a loopback callback and
  refreshes tokens in a single-flight custom fetch wrapper.
- Approved raw access uses only fresh catalog-derived, explicitly priced routes;
  no unrestricted OpenRouter wildcard route is exposed. Stable aliases are explicit.
- Policy fails closed; unknown groups and unavailable live catalogs do not expand access.
- Clients cannot replace server-managed fallback routes, and every deployment or
  fallback chain must stay within one OpenRouter/direct privacy class.

## Known blockers

- A real OpenRouter request cannot be made without `OPENROUTER_API_KEY`.
- Corporate end-to-end SSO cannot be certified without the target IdP configuration.
- Example budget values are illustrative and require organizational approval.
- Live-provider and corporate-IdP certification require protected deployment
  credentials and an approved test environment.
- OpenCode 1.18.26 one-shot `opencode run` startup can intermittently remain in
  upstream initialization; the long-lived server/session path passed, and the
  one-shot behavior remains a client rollout gate.
- Horizontal gateway scaling requires a tested shared coordination Redis; the
  current single-worker topology does not claim aggregate multi-replica RPM/TPM.
