import { get } from "node:http"
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

import type { Config, PluginInput } from "@opencode-ai/plugin"
import { afterEach, describe, expect, it, vi } from "vitest"

import { configureProvider, loadCatalog, refreshCatalog } from "../src/catalog.js"
import plugin, { OrganizationPlugin } from "../src/index.js"
import { beginAuthorization } from "../src/oauth.js"
import { readOptions } from "../src/options.js"
import { createAuthenticatedFetch } from "../src/token-fetch.js"

const baseOptions = () => readOptions({
  gatewayUrl: "http://127.0.0.1:4000/v1",
  issuer: "http://127.0.0.1:8080/realms/test",
  clientId: "opencode-test",
  allowInsecureLocalhost: true,
})

afterEach(() => vi.restoreAllMocks())

describe("provider configuration", () => {
  it("exports the upstream plugin module shape", () => {
    expect(plugin).toEqual({ id: "organization.opencode-ai", server: OrganizationPlugin })
  })

  it("loads with packaged aliases before first login", async () => {
    const directory = await mkdtemp(join(tmpdir(), "enterprise-ai-plugin-"))
    const options = readOptions({ ...baseOptions(), cachePath: join(directory, "missing.json") })
    const models = await loadCatalog(options)
    const config: Config = {}
    configureProvider(config, options, models)
    expect(Object.keys(config.provider?.organization?.models ?? {})).toContain("coding-frontier")
    expect(config.provider?.organization?.models?.["coding-frontier"]?.tool_call).toBe(true)
    expect(config.model).toBe("organization/coding-frontier")
    await rm(directory, { recursive: true })
  })

  it("uses packaged aliases when the cache contains no valid models", async () => {
    const directory = await mkdtemp(join(tmpdir(), "enterprise-ai-plugin-"))
    const cachePath = join(directory, "models.json")
    await writeFile(cachePath, JSON.stringify({
      schema_version: 1,
      fetched_at: new Date().toISOString(),
      models: [{ id: "vendor/*", name: "Unsafe wildcard" }],
    }))
    const options = readOptions({ ...baseOptions(), cachePath })
    const models = await loadCatalog(options)
    const config: Config = {}
    configureProvider(config, options, models)
    expect(Object.keys(config.provider?.organization?.models ?? {})).toContain("coding-frontier")
    expect(config.provider?.organization?.models?.["coding-frontier"]?.tool_call).toBe(true)
    await rm(directory, { recursive: true })
  })

  it("atomically caches models returned by the authenticated gateway", async () => {
    const directory = await mkdtemp(join(tmpdir(), "enterprise-ai-plugin-"))
    const cachePath = join(directory, "catalog", "models.json")
    const options = readOptions({ ...baseOptions(), cachePath })
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({
      data: [{ id: "vendor/*", name: "Unsafe wildcard" }, { id: "general-fast", name: "Fast" }],
    })))
    await refreshCatalog(options, "access-token")
    const cachedDocument = JSON.parse(await readFile(cachePath, "utf8"))
    expect(cachedDocument.models).toHaveLength(1)
    const cached = cachedDocument.models[0]
    expect(cached.id).toBe("general-fast")
    expect(cached.supported_parameters).toContain("tools")
    await rm(directory, { recursive: true })
  })
})

describe("OIDC Authorization Code + PKCE", () => {
  it("validates state and exchanges the callback code", async () => {
    const options = baseOptions()
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url
      if (url.endsWith(".well-known/openid-configuration")) {
        return Response.json({
          issuer: options.issuer,
          authorization_endpoint: `${options.issuer}/authorize`,
          token_endpoint: `${options.issuer}/token`,
        })
      }
      expect(new URLSearchParams(String(init?.body)).get("code_verifier")).toBeTruthy()
      expect(new URLSearchParams(String(init?.body)).get("code")).toBe("callback-code")
      return Response.json({ access_token: "access", refresh_token: "refresh", expires_in: 300 })
    })
    vi.stubGlobal("fetch", fetchMock)
    const pending = await beginAuthorization(options)
    const authorization = new URL(pending.url)
    expect(authorization.searchParams.get("code_challenge_method")).toBe("S256")
    const redirect = new URL(authorization.searchParams.get("redirect_uri")!)
    redirect.searchParams.set("state", authorization.searchParams.get("state")!)
    redirect.searchParams.set("code", "callback-code")
    const exchange = pending.exchange()
    await new Promise<void>((resolve, reject) => get(redirect, (response) => {
      response.resume()
      response.on("end", resolve)
    }).on("error", reject))
    await expect(exchange).resolves.toMatchObject({ access_token: "access", refresh_token: "refresh" })
    await pending.close()
  })
})

describe("authenticated provider fetch", () => {
  it("refreshes once, persists rotation, and injects identity", async () => {
    const options = baseOptions()
    const set = vi.fn(async () => ({ data: true }))
    const input = { client: { auth: { set } } } as unknown as PluginInput
    const auth = async () => ({ type: "oauth" as const, access: "old", refresh: "refresh-old", expires: 0 })
    const fetchMock = vi.fn(async (request: string | URL | Request, init?: RequestInit) => {
      const url = typeof request === "string" ? request : request instanceof URL ? request.toString() : request.url
      if (url.endsWith(".well-known/openid-configuration")) {
        return Response.json({
          issuer: options.issuer,
          authorization_endpoint: `${options.issuer}/authorize`,
          token_endpoint: `${options.issuer}/token`,
        })
      }
      if (url.endsWith("/token")) return Response.json({ access_token: "new", refresh_token: "refresh-new", expires_in: 3600 })
      const headers = new Headers(init?.headers)
      expect(headers.get("authorization")).toBe("Bearer new")
      expect(headers.get("x-enterprise-ai-client")).toBe("opencode")
      return new Response("ok")
    })
    vi.stubGlobal("fetch", fetchMock)
    const managedFetch = createAuthenticatedFetch(input, options, auth)
    await expect((await managedFetch("http://127.0.0.1:4000/v1/chat/completions")).text()).resolves.toBe("ok")
    expect(set).toHaveBeenCalledOnce()
  })

  it("returns streaming responses untouched and refuses token exfiltration", async () => {
    const options = baseOptions()
    const input = { client: { auth: { set: vi.fn() } } } as unknown as PluginInput
    const auth = async () => ({
      type: "oauth" as const,
      access: "live",
      refresh: "refresh",
      expires: Date.now() + 3600_000,
    })
    vi.stubGlobal("fetch", vi.fn(async () => new Response("data: {\"delta\":\"hello\"}\n\ndata: [DONE]\n\n")))
    const managedFetch = createAuthenticatedFetch(input, options, auth)
    const response = await managedFetch("http://127.0.0.1:4000/v1/chat/completions")
    expect(await response.text()).toContain("[DONE]")
    await expect(managedFetch("https://attacker.example/v1/chat/completions")).rejects.toThrow("refusing")
  })
})
