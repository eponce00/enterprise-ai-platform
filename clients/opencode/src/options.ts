import { homedir } from "node:os"
import { join } from "node:path"

export interface OrganizationPluginOptions {
  providerId: string
  displayName: string
  gatewayUrl: string
  issuer: string
  clientId: string
  scope: string
  audience: string | undefined
  cachePath: string
  redirectPort: number
  exclusiveProvider: boolean
  allowInsecureLocalhost: boolean
  refreshSkewMs: number
  requestTimeoutMs: number
}

type RawOptions = Record<string, unknown>

const MAX_REQUEST_TIMEOUT_MS = 300_000

export function readOptions(raw: RawOptions = {}): OrganizationPluginOptions {
  const gatewayUrl = stringOption(raw, "gatewayUrl", process.env.ENTERPRISE_AI_GATEWAY_URL ?? "")
  const issuer = stringOption(raw, "issuer", process.env.ENTERPRISE_AI_OIDC_ISSUER ?? "").replace(/\/$/, "")
  const clientId = stringOption(raw, "clientId", process.env.ENTERPRISE_AI_OIDC_CLIENT_ID ?? "")
  if (!gatewayUrl || !issuer || !clientId) {
    throw new Error("gatewayUrl, issuer, and clientId are required for the organization plugin")
  }
  const allowInsecureLocalhost = booleanOption(raw, "allowInsecureLocalhost", isLocalHttp(gatewayUrl) && isLocalHttp(issuer))
  for (const [name, value] of [["gatewayUrl", gatewayUrl], ["issuer", issuer]] as const) {
    const url = new URL(value)
    if (url.protocol !== "https:" && !(allowInsecureLocalhost && isLoopback(url.hostname))) {
      throw new Error(`${name} must use HTTPS (HTTP is allowed only on loopback for development)`)
    }
  }

  return {
    providerId: stringOption(raw, "providerId", "organization"),
    displayName: stringOption(raw, "displayName", "Organization AI"),
    gatewayUrl: gatewayUrl.replace(/\/$/, ""),
    issuer,
    clientId,
    scope: stringOption(raw, "scope", process.env.ENTERPRISE_AI_OIDC_SCOPE ?? "openid profile email offline_access"),
    audience: optionalString(raw.audience ?? process.env.ENTERPRISE_AI_OIDC_AUDIENCE),
    cachePath: stringOption(raw, "cachePath", defaultCachePath()),
    redirectPort: numberOption(raw, "redirectPort", 0),
    exclusiveProvider: booleanOption(raw, "exclusiveProvider", false),
    allowInsecureLocalhost,
    refreshSkewMs: numberOption(raw, "refreshSkewMs", 120_000),
    requestTimeoutMs: boundedPositiveNumberOption(
      raw,
      "requestTimeoutMs",
      15_000,
      MAX_REQUEST_TIMEOUT_MS,
    ),
  }
}

function defaultCachePath(): string {
  const base = process.platform === "win32"
    ? process.env.LOCALAPPDATA ?? join(homedir(), "AppData", "Local")
    : process.env.XDG_CACHE_HOME ?? join(homedir(), ".cache")
  return join(base, "enterprise-ai", "opencode", "models.json")
}

function stringOption(raw: RawOptions, key: string, fallback: string): string {
  const value = raw[key]
  if (value === undefined) return fallback
  if (typeof value !== "string" || !value.trim()) throw new Error(`${key} must be a non-empty string`)
  return value.trim()
}

function optionalString(value: unknown): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  if (typeof value !== "string") throw new Error("audience must be a string")
  return value
}

function numberOption(raw: RawOptions, key: string, fallback: number): number {
  const value = raw[key]
  if (value === undefined) return fallback
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) throw new Error(`${key} must be a non-negative integer`)
  return value
}

function positiveNumberOption(raw: RawOptions, key: string, fallback: number): number {
  const value = numberOption(raw, key, fallback)
  if (value === 0) throw new Error(`${key} must be a positive integer`)
  return value
}

function boundedPositiveNumberOption(
  raw: RawOptions,
  key: string,
  fallback: number,
  maximum: number,
): number {
  const value = positiveNumberOption(raw, key, fallback)
  if (value > maximum) throw new Error(`${key} must be at most ${maximum}`)
  return value
}

function booleanOption(raw: RawOptions, key: string, fallback: boolean): boolean {
  const value = raw[key]
  if (value === undefined) return fallback
  if (typeof value !== "boolean") throw new Error(`${key} must be a boolean`)
  return value
}

function isLocalHttp(value: string): boolean {
  try {
    const url = new URL(value)
    return url.protocol === "http:" && isLoopback(url.hostname)
  } catch {
    return false
  }
}

function isLoopback(hostname: string): boolean {
  return hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]" || hostname === "::1"
}
