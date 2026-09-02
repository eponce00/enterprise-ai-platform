import { createHash, randomBytes, randomUUID } from "node:crypto"
import { createServer, type Server } from "node:http"
import type { AddressInfo } from "node:net"

import type { OrganizationPluginOptions } from "./options.js"

export interface OIDCMetadata {
  issuer: string
  authorization_endpoint: string
  token_endpoint: string
}

export interface OAuthTokens {
  access_token: string
  refresh_token?: string
  expires_in?: number
}

interface PendingCallback {
  redirectUri: string
  wait: Promise<string>
  close: () => Promise<void>
}

export async function discover(options: OrganizationPluginOptions): Promise<OIDCMetadata> {
  const response = await fetch(`${options.issuer}/.well-known/openid-configuration`, { redirect: "error" })
  if (!response.ok) throw new Error(`OIDC discovery failed with HTTP ${response.status}`)
  const metadata = await response.json() as Partial<OIDCMetadata>
  if (metadata.issuer?.replace(/\/$/, "") !== options.issuer) throw new Error("OIDC discovery issuer mismatch")
  if (!metadata.authorization_endpoint || !metadata.token_endpoint) throw new Error("OIDC discovery lacks required endpoints")
  assertSecureEndpoint(metadata.authorization_endpoint, options)
  assertSecureEndpoint(metadata.token_endpoint, options)
  return metadata as OIDCMetadata
}

export async function beginAuthorization(options: OrganizationPluginOptions): Promise<{
  url: string
  exchange: () => Promise<OAuthTokens>
  close: () => Promise<void>
}> {
  const metadata = await discover(options)
  const verifier = base64Url(randomBytes(32))
  const challenge = base64Url(createHash("sha256").update(verifier).digest())
  const state = randomUUID()
  const callback = await listenForCode(state, options.redirectPort)
  const url = new URL(metadata.authorization_endpoint)
  url.searchParams.set("response_type", "code")
  url.searchParams.set("client_id", options.clientId)
  url.searchParams.set("redirect_uri", callback.redirectUri)
  url.searchParams.set("scope", options.scope)
  url.searchParams.set("state", state)
  url.searchParams.set("code_challenge", challenge)
  url.searchParams.set("code_challenge_method", "S256")
  if (options.audience) url.searchParams.set("audience", options.audience)

  return {
    url: url.toString(),
    close: callback.close,
    exchange: async () => {
      const code = await callback.wait
      return exchangeCode(metadata.token_endpoint, options, {
        grant_type: "authorization_code",
        client_id: options.clientId,
        code,
        redirect_uri: callback.redirectUri,
        code_verifier: verifier,
      })
    },
  }
}

export async function refreshTokens(options: OrganizationPluginOptions, refreshToken: string): Promise<OAuthTokens> {
  if (!refreshToken) throw new Error("the OIDC session has no refresh token; sign in again")
  const metadata = await discover(options)
  return exchangeCode(metadata.token_endpoint, options, {
    grant_type: "refresh_token",
    client_id: options.clientId,
    refresh_token: refreshToken,
  })
}

async function exchangeCode(
  endpoint: string,
  options: OrganizationPluginOptions,
  fields: Record<string, string>,
): Promise<OAuthTokens> {
  assertSecureEndpoint(endpoint, options)
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(fields),
    redirect: "error",
  })
  if (!response.ok) throw new Error(`OIDC token exchange failed with HTTP ${response.status}`)
  const tokens = await response.json() as OAuthTokens
  if (!tokens.access_token) throw new Error("OIDC token response has no access token")
  return tokens
}

async function listenForCode(expectedState: string, port: number): Promise<PendingCallback> {
  let resolveCode!: (code: string) => void
  let rejectCode!: (error: Error) => void
  const wait = new Promise<string>((resolve, reject) => {
    resolveCode = resolve
    rejectCode = reject
  })
  let settled = false
  const server = createServer((request, response) => {
    try {
      const url = new URL(request.url ?? "/", "http://127.0.0.1")
      if (url.pathname !== "/oauth/callback") {
        response.writeHead(404).end("Not found")
        return
      }
      if (url.searchParams.get("state") !== expectedState) {
        response.writeHead(400).end("OAuth state mismatch")
        if (!settled) rejectCode(new Error("OAuth state mismatch"))
        settled = true
        return
      }
      const error = url.searchParams.get("error")
      const code = url.searchParams.get("code")
      if (error || !code) {
        response.writeHead(400).end("Sign-in failed; return to OpenCode")
        if (!settled) rejectCode(new Error(error ? `OIDC authorization failed: ${error}` : "OIDC callback has no code"))
        settled = true
        return
      }
      response.writeHead(200, { "content-type": "text/plain; charset=utf-8" }).end("Sign-in complete. You can close this tab.")
      if (!settled) resolveCode(code)
      settled = true
    } catch (error) {
      response.writeHead(400).end("Invalid callback")
      if (!settled) rejectCode(error instanceof Error ? error : new Error("Invalid callback"))
      settled = true
    }
  })
  server.on("error", (error) => {
    if (!settled) rejectCode(error)
    settled = true
  })
  await new Promise<void>((resolve, reject) => {
    server.listen(port, "127.0.0.1", resolve)
    server.once("error", reject)
  })
  const address = server.address() as AddressInfo
  const timer = setTimeout(() => {
    if (!settled) rejectCode(new Error("OIDC authorization timed out"))
    settled = true
    void closeServer(server)
  }, 180_000)
  timer.unref()
  return {
    redirectUri: `http://127.0.0.1:${address.port}/oauth/callback`,
    wait,
    close: async () => {
      clearTimeout(timer)
      if (!settled) rejectCode(new Error("OIDC authorization cancelled"))
      settled = true
      await closeServer(server)
    },
  }
}

function closeServer(server: Server): Promise<void> {
  return new Promise((resolve) => {
    if (!server.listening) return resolve()
    server.close(() => resolve())
  })
}

function assertSecureEndpoint(endpoint: string, options: OrganizationPluginOptions): void {
  const url = new URL(endpoint)
  const loopback = url.hostname === "localhost" || url.hostname === "127.0.0.1" || url.hostname === "[::1]"
  if (url.protocol !== "https:" && !(options.allowInsecureLocalhost && loopback)) {
    throw new Error(`refusing insecure OIDC endpoint: ${url.origin}`)
  }
}

function base64Url(value: Uint8Array): string {
  return Buffer.from(value).toString("base64url")
}
