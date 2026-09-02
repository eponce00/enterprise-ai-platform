import type { PluginInput } from "@opencode-ai/plugin"
import type { Auth } from "@opencode-ai/sdk/v2"

import { refreshCatalog } from "./catalog.js"
import { refreshTokens } from "./oauth.js"
import type { OrganizationPluginOptions } from "./options.js"

interface TokenState {
  access: string
  refresh: string
  expires: number
}

export function createAuthenticatedFetch(
  input: PluginInput,
  options: OrganizationPluginOptions,
  getAuth: () => Promise<Auth>,
): typeof fetch {
  let refreshInFlight: Promise<TokenState> | undefined

  const renew = async (auth: Extract<Auth, { type: "oauth" }>): Promise<TokenState> => {
    if (!refreshInFlight) {
      refreshInFlight = refreshTokens(options, auth.refresh)
        .then(async (tokens) => {
          const state = {
            access: tokens.access_token,
            refresh: tokens.refresh_token ?? auth.refresh,
            expires: Date.now() + (tokens.expires_in ?? 3600) * 1000,
          }
          await input.client.auth.set({
            path: { id: options.providerId },
            body: { type: "oauth", ...state },
          })
          void refreshCatalog(options, state.access).catch(() => undefined)
          return state
        })
        .finally(() => { refreshInFlight = undefined })
    }
    return refreshInFlight
  }

  const current = async (force = false): Promise<TokenState> => {
    const auth = await getAuth()
    if (auth.type !== "oauth") throw new Error("organization provider is not signed in")
    if (force || !auth.expires || auth.expires <= Date.now() + options.refreshSkewMs) return renew(auth)
    return auth
  }

  return async (requestInput: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const url = requestUrl(requestInput)
    const gateway = new URL(options.gatewayUrl)
    if (url.origin !== gateway.origin || !pathWithin(url.pathname, gateway.pathname)) {
      throw new Error(`refusing to send the organization OAuth token to ${url.origin}`)
    }
    const retryInput = requestInput instanceof Request ? requestInput.clone() : requestInput
    const first = await current()
    let response = await fetchWithToken(requestInput, init, first.access)
    if (response.status === 401 && first.refresh) {
      const next = await current(true)
      response = await fetchWithToken(retryInput, init, next.access)
    }
    return response
  }
}

async function fetchWithToken(
  requestInput: string | URL | Request,
  init: RequestInit | undefined,
  token: string,
): Promise<Response> {
  const headers = new Headers(requestInput instanceof Request ? requestInput.headers : undefined)
  new Headers(init?.headers).forEach((value, key) => headers.set(key, value))
  headers.set("Authorization", `Bearer ${token}`)
  headers.set("X-Enterprise-AI-Client", "opencode")
  // A gateway redirect is a deployment error; never carry the OIDC bearer
  // through an unexpected redirect chain.
  return fetch(requestInput, { ...init, headers, redirect: "error" })
}

function requestUrl(input: string | URL | Request): URL {
  if (input instanceof URL) return input
  return new URL(typeof input === "string" ? input : input.url)
}

function pathWithin(pathname: string, basePath: string): boolean {
  const normalized = basePath.endsWith("/") ? basePath : `${basePath}/`
  return pathname === basePath || pathname.startsWith(normalized)
}
