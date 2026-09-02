# Organization OpenCode plugin

Target: official OpenCode `1.18.26` (stable V1). The package uses only public
MIT-licensed plugin hooks; it does not patch or fork OpenCode.

```sh
opencode plugin @organization/opencode-ai@0.1.0 --global
```

An organization should rename the package before publishing it to its private
registry. Registry credentials belong in normal npm configuration, never in
OpenCode config.

Before login, replace the generated string-only plugin entry with a configured
tuple. The plugin has no generic gateway or identity-provider defaults:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": [[
    "@organization/opencode-ai@0.1.0",
    {
      "gatewayUrl": "https://gateway.example.com/v1",
      "issuer": "https://identity.example.com",
      "clientId": "opencode-cli",
      "scope": "openid profile email offline_access",
      "redirectPort": 8765,
      "allowInsecureLocalhost": false,
      "exclusiveProvider": true
    }
  ]]
}
```

Then authenticate:

```sh
opencode auth login --provider organization --method "Company SSO"
```

The fixed `redirectPort` must match the native-client loopback URI registered at
the identity provider.

For a source-tree smoke test, run `npm ci` and
`npm run build --workspace clients/opencode`, then set the tuple's first element
to `file:///absolute/path/to/repo/clients/opencode/dist/index.js` (or a
`file:///C:/...` URL on Windows). A `.tgz` is an npm package spec, not a
JavaScript module entry point; leaving `file:/...tgz` in runtime configuration
invokes OpenCode's package-install path. To test the packed artifact, first
install it and its production dependencies into an isolated directory, then
point the tuple at the installed `dist/index.js`. Production deployments should
use an exact immutable version from the organization's private registry.

The plugin uses Authorization Code + PKCE and a loopback callback bound only to
`127.0.0.1`. OpenCode stores the OAuth access/refresh pair. A single-flight
custom fetch refreshes it early, persists rotation, verifies the destination
origin/path, replaces the Authorization header, and passes streaming responses
through unchanged.

The model catalog is last-known-good data in the user's normal OS cache. It is
refreshed after login/token refresh and read by the next OpenCode startup. Five
logical aliases are packaged as the offline fallback. A documented OpenCode
1.18.26 ordering limitation prevents relying on `provider.models` for a newly
introduced custom provider, so catalog population intentionally occurs in the
`config` hook.
