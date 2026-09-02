# OpenCode setup

Install official OpenCode 1.18.26 through its supported installer, then install
the organization-published plugin:

```sh
opencode plugin @organization/opencode-ai@0.1.0 --global
opencode auth login --provider organization --method "Company SSO"
```

Run `/models` to choose an approved model. Logical aliases remain available if
the catalog cannot refresh. When a session expires, the plugin refreshes early;
if the refresh token is revoked or missing, rerun the login command.

Organizations should rename the placeholder npm scope and publish to a private
registry. Use normal npm registry authentication. The full configuration fallback
is in `clients/opencode/example-config/opencode.json`.

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
Run an interactive staging login before any client rollout.
