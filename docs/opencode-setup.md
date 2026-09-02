# OpenCode setup

Install official OpenCode 1.18.26 through its supported installer, then install
the organization-published plugin:

```sh
opencode plugin @organization/opencode-ai@0.1.0 --global
```

Before login, the platform administrator must replace the generated string-only
plugin entry with the managed tuple from
`clients/opencode/example-config/opencode.json`, populated with the production
gateway URL, OIDC issuer, and client ID. The plugin deliberately has no generic
network defaults. Once that configuration is present, run:

```sh
opencode auth login --provider organization --method "Company SSO"
```

Register OpenCode as a public/native OIDC client with no client secret. Require
Authorization Code with PKCE S256, and disable implicit and password/direct
grants. Either set a managed `redirectPort` and register exactly
`http://127.0.0.1:<port>/oauth/callback`, or explicitly verify that the IdP
supports dynamic loopback ports as described by RFC 8252. Production gateway and
issuer URLs must use HTTPS; set `allowInsecureLocalhost:false`. Configure the
plugin's `audience` option only when the target IdP requires an audience or
resource parameter in the authorization request.

Run `/models` to choose an approved model. Logical aliases remain available if
the catalog cannot refresh. When a session expires, the plugin refreshes early;
if the refresh token is revoked or missing, rerun the login command. When
refresh-token rotation is required, configure the production IdP to allow
`offline_access` for the registered OpenCode client and the applicable user
authorization policy.

OpenCode's auth store contains bearer and refresh credentials. Protect the user
profile with normal per-user permissions and device encryption, and revoke the
IdP session when a device is lost or a user is offboarded.

Organizations should rename the placeholder npm scope and publish to a private
registry. Use normal npm registry authentication. Environment variables with
the `ENTERPRISE_AI_` prefix are supported for staging automation, but managed
OpenCode configuration is the recommended user rollout path.

For centrally managed installations, administrators can also set
`OPENCODE_DISABLE_AUTOUPDATE=1` and `OPENCODE_DISABLE_MODELS_FETCH=1` so client
upgrades and the public OpenCode model catalog do not change independently of
the approved release. These controls improve reproducibility; they do not
replace an organizational egress policy or the gateway's server-side model
allowlist. Deploy them through normal device/process management if they are a
rollout requirement; a device owner can otherwise unset them.

Set `exclusiveProvider:true` in managed configuration to hide other providers
from this OpenCode installation. This is an accidental-use control, not a device
security boundary: laptop owners can install other clients or use personal keys.
They cannot extract company inference-provider credentials because those remain
on the gateway.

The implementation uses stable V1 public hooks. V2 remained beta at the
2026-09-02 compatibility review and is not source-compatible. CI installs the
local package into the pinned official OpenCode binary and verifies that the
provider and fallback models load. Isolated plugin tests cover PKCE, refresh,
destination guarding, catalog caching, streaming pass-through, and tool metadata;
the mock Compose suite covers gateway completions, streaming, and tool calls.
Run an interactive staging login, a bounded `opencode run` smoke, and a local
server/session smoke before any client rollout; the pinned client's known
startup behavior is recorded in the
[compatibility review](upstream-compatibility.md).
