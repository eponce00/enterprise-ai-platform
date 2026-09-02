import { mkdir, readFile, rename, writeFile } from "node:fs/promises"
import { dirname } from "node:path"

import type { Config } from "@opencode-ai/plugin"
import type { OrganizationPluginOptions } from "./options.js"

interface GatewayModel {
  id: string
  name?: string
  context_length?: number
  max_output_tokens?: number
  supported_parameters?: string[]
  input_modalities?: Array<"text" | "audio" | "image" | "video" | "pdf">
  output_modalities?: Array<"text" | "audio" | "image" | "video" | "pdf">
  pricing_usd_per_million?: { prompt?: number, completion?: number }
}

interface CacheDocument {
  schema_version: 1
  fetched_at: string
  models: GatewayModel[]
}

const FALLBACK_MODELS: GatewayModel[] = [
  { id: "coding-frontier", name: "Coding — frontier", context_length: 128_000, max_output_tokens: 16_384, supported_parameters: ["tools"] },
  { id: "coding-fast", name: "Coding — fast", context_length: 128_000, max_output_tokens: 16_384, supported_parameters: ["tools"] },
  { id: "general-fast", name: "General — fast", context_length: 128_000, max_output_tokens: 16_384, supported_parameters: ["tools"] },
  { id: "general-quality", name: "General — quality", context_length: 128_000, max_output_tokens: 16_384, supported_parameters: ["tools"] },
  { id: "cheap-batch", name: "Batch — economical", context_length: 64_000, max_output_tokens: 8_192 },
]

export async function loadCatalog(options: OrganizationPluginOptions): Promise<GatewayModel[]> {
  try {
    const document = JSON.parse(await readFile(options.cachePath, "utf8")) as Partial<CacheDocument>
    if (document.schema_version === 1 && Array.isArray(document.models) && document.models.length) {
      return document.models.filter(validModel)
    }
  } catch {
    // First run and a corrupt cache both use the packaged, safe aliases.
  }
  return FALLBACK_MODELS
}

export async function refreshCatalog(options: OrganizationPluginOptions, accessToken: string): Promise<void> {
  const response = await fetch(`${options.gatewayUrl}/models`, {
    headers: { Authorization: `Bearer ${accessToken}`, "X-Enterprise-AI-Client": "opencode" },
    redirect: "error",
  })
  if (!response.ok) throw new Error(`gateway catalog request failed with HTTP ${response.status}`)
  const body = await response.json() as { data?: GatewayModel[] }
  if (!Array.isArray(body.data) || !body.data.length) throw new Error("gateway returned an empty model catalog")
  // LiteLLM's OpenAI-compatible /models shape is intentionally sparse. Keep
  // packaged capability metadata for stable aliases when refreshing IDs so a
  // successful refresh cannot accidentally disable tool calling next launch.
  const fallbackById = new Map(FALLBACK_MODELS.map((model) => [model.id, model]))
  const models = body.data.filter(validModel).map((model) => ({ ...fallbackById.get(model.id), ...model }))
  if (!models.length) throw new Error("gateway returned no valid models")
  const document: CacheDocument = { schema_version: 1, fetched_at: new Date().toISOString(), models }
  await mkdir(dirname(options.cachePath), { recursive: true, mode: 0o700 })
  const temporary = `${options.cachePath}.${process.pid}.${Date.now()}.tmp`
  await writeFile(temporary, `${JSON.stringify(document, null, 2)}\n`, { encoding: "utf8", mode: 0o600 })
  await rename(temporary, options.cachePath)
}

export function configureProvider(
  config: Config,
  options: OrganizationPluginOptions,
  models: GatewayModel[],
): void {
  config.provider ??= {}
  config.provider[options.providerId] = {
    name: options.displayName,
    npm: "@ai-sdk/openai-compatible",
    options: { baseURL: options.gatewayUrl },
    models: Object.fromEntries(models.map((model) => [model.id, toOpenCodeModel(model)])),
  }
  config.model ??= `${options.providerId}/coding-frontier`
  config.small_model ??= `${options.providerId}/general-fast`
  if (options.exclusiveProvider) config.enabled_providers = [options.providerId]
}

function toOpenCodeModel(model: GatewayModel) {
  const supported = new Set(model.supported_parameters ?? [])
  return {
    name: model.name ?? model.id,
    tool_call: supported.has("tools"),
    reasoning: supported.has("reasoning"),
    attachment: (model.input_modalities ?? []).some((item) => item !== "text"),
    limit: {
      context: model.context_length ?? 128_000,
      output: model.max_output_tokens ?? 16_384,
    },
    modalities: {
      input: model.input_modalities ?? ["text" as const],
      output: model.output_modalities ?? ["text" as const],
    },
    cost: {
      input: model.pricing_usd_per_million?.prompt ?? 0,
      output: model.pricing_usd_per_million?.completion ?? 0,
    },
  }
}

function validModel(value: unknown): value is GatewayModel {
  if (!value || typeof value !== "object" || typeof (value as GatewayModel).id !== "string") return false
  const id = (value as GatewayModel).id
  return Boolean(id && !["*", "?", "[", "]"].some((character) => id.includes(character)))
}
