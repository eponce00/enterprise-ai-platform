import type { Plugin, PluginModule } from "@opencode-ai/plugin"

import { configureProvider, loadCatalog, refreshCatalog } from "./catalog.js"
import { beginAuthorization } from "./oauth.js"
import { readOptions } from "./options.js"
import { createAuthenticatedFetch } from "./token-fetch.js"

export const OrganizationPlugin: Plugin = async (input, rawOptions = {}) => {
  const options = readOptions(rawOptions)
  return {
    config: async (config) => {
      configureProvider(config, options, await loadCatalog(options))
    },
    auth: {
      provider: options.providerId,
      loader: async (getAuth) => ({
        // The AI SDK requires a non-empty key. The custom fetch replaces it.
        apiKey: "oidc-managed-by-organization-plugin",
        fetch: createAuthenticatedFetch(input, options, getAuth),
      }),
      methods: [
        {
          type: "oauth",
          label: "Company SSO",
          authorize: async () => {
            const pending = await beginAuthorization(options)
            return {
              method: "auto" as const,
              url: pending.url,
              instructions: "Complete sign-in in your browser. OpenCode will continue automatically.",
              callback: async () => {
                try {
                  const tokens = await pending.exchange()
                  if (!tokens.refresh_token) return { type: "failed" as const }
                  const result = {
                    type: "success" as const,
                    access: tokens.access_token,
                    refresh: tokens.refresh_token,
                    expires: Date.now() + (tokens.expires_in ?? 3600) * 1000,
                  }
                  await refreshCatalog(options, result.access).catch(() => undefined)
                  return result
                } catch {
                  return { type: "failed" as const }
                } finally {
                  await pending.close()
                }
              },
            }
          },
        },
      ],
    },
    "chat.headers": async (_request, output) => {
      output.headers["X-Enterprise-AI-Client"] = "opencode"
    },
  }
}

export default {
  id: "organization.opencode-ai",
  server: OrganizationPlugin,
} satisfies PluginModule

export { configureProvider, loadCatalog, refreshCatalog } from "./catalog.js"
export { beginAuthorization, discover, refreshTokens } from "./oauth.js"
export { readOptions } from "./options.js"
export { createAuthenticatedFetch } from "./token-fetch.js"
