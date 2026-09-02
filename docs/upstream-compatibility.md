# Upstream compatibility review

Reviewed 2026-09-02.

## LiteLLM 1.99.0

The OSS `general_settings.custom_auth` function accepts FastAPI `Request` and a
bearer value and returns `UserAPIKeyAuth`. `custom_auth_run_common_checks:true`
enables database-backed team/user/project checks;
`enable_post_custom_auth_checks:true` enables returned-object expiry/model checks.
Native JWT auth and custom-auth-to-virtual-key mapping are Enterprise features and
are not used. Sources: [custom auth guide](https://docs.litellm.ai/docs/proxy/custom_auth),
[tagged auth source](https://github.com/BerriAI/litellm/blob/v1.99.0/litellm/proxy/auth/user_api_key_auth.py),
[license](https://github.com/BerriAI/litellm/blob/v1.99.0/LICENSE).

The official 1.99.0 image is mixed-license and includes `enterprise/`, although
the selected hooks require no enterprise key. This is documented rather than
silently described as a pure-MIT artifact.

LiteLLM supports `openrouter/*`, but this deployment intentionally renders
explicit approved routes instead: an unrestricted wildcard would bypass catalog
filtering. Source: [wildcard routing](https://docs.litellm.ai/docs/wildcard_routing).

## OpenCode 1.18.26

Stable V1 exposes `config`, OAuth `auth`, custom provider options/fetch, and
per-request header hooks. The custom fetch can obtain current auth, refresh,
persist rotation, and preserve SSE. No inference loopback proxy is needed. The
loopback listener is only the OAuth redirect receiver. Sources:
[tagged plugin types](https://github.com/anomalyco/opencode/blob/v1.18.26/packages/plugin/src/index.ts),
[official fetch precedent](https://github.com/anomalyco/opencode/blob/v1.18.26/packages/opencode/src/plugin/xai.ts),
[MIT license](https://github.com/anomalyco/opencode/blob/v1.18.26/LICENSE).

A 1.18.26 ordering limitation skips `provider.models` for a brand-new custom
provider, so the plugin reads an atomically cached catalog in its `config` hook.
The cache refreshes after login/refresh and applies on the next OpenCode instance.
V2 remained beta and was not chosen.

An isolated two-host staging run also found that the 1.18.26 one-shot
`opencode run` command can intermittently remain in its upstream `init` phase
before it creates a session. The same pinned build's long-lived local server and
session API completed an authenticated streaming request and one built-in
read-tool round trip through the plugin. Treat
one-shot startup as a client rollout gate: use a bounded smoke-test timeout,
retain diagnostics on failure, and validate a newer pinned release before
upgrading. Related upstream reports include
[#40516](https://github.com/anomalyco/opencode/issues/40516) and
[#42779](https://github.com/anomalyco/opencode/issues/42779).

For an unpackaged local smoke test, point the plugin tuple directly at the built
module with an absolute `file:///.../dist/index.js` URL. A `file:/...tgz` entry
uses OpenCode's package-install path and can make startup depend on npm registry
availability; see upstream reports
[#41934](https://github.com/anomalyco/opencode/issues/41934) and
[#31463](https://github.com/anomalyco/opencode/issues/31463). Published rollouts
should use the exact private-registry package version instead.

## OpenRouter

Use authenticated `/api/v1/models/user` for the account-effective catalog and
public `/api/v1/models?zdr=true` only without credentials. Server policy maps to
`provider.zdr`, `data_collection`, `only`, `ignore`, and `require_parameters`.
Account guardrails are the non-overridable floor. Sources:
[models](https://openrouter.ai/docs/api/api-reference/models/get-models),
[provider routing](https://openrouter.ai/docs/guides/routing/provider-selection),
[ZDR](https://openrouter.ai/docs/guides/features/zdr), and
[data collection](https://openrouter.ai/docs/guides/privacy/data-collection).

Model IDs change rapidly. Current family examples belong only in opt-in test
configuration, never architectural policy.
