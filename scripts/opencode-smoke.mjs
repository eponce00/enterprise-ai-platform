import { mkdtemp, readFile, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { spawnSync } from "node:child_process"
import { fileURLToPath } from "node:url"

const repository = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const temporary = await mkdtemp(join(tmpdir(), "enterprise-ai-opencode-"))
const npmCli = process.env.npm_execpath

if (!npmCli) throw new Error("npm_execpath is required; run this smoke test through npm")

const environment = {
  ...process.env,
  XDG_CONFIG_HOME: join(temporary, "config"),
  XDG_DATA_HOME: join(temporary, "data"),
  XDG_CACHE_HOME: join(temporary, "cache"),
  XDG_STATE_HOME: join(temporary, "state"),
  ENTERPRISE_AI_GATEWAY_URL: "http://127.0.0.1:4000/v1",
  ENTERPRISE_AI_OIDC_ISSUER: "http://127.0.0.1:8080/realms/enterprise-ai",
  ENTERPRISE_AI_OIDC_CLIENT_ID: "enterprise-ai-cli",
}

function opencode(...arguments_) {
  const result = spawnSync(
    process.execPath,
    [npmCli, "exec", "--", "opencode", ...arguments_],
    { cwd: repository, env: environment, encoding: "utf8", timeout: 120_000 },
  )
  if (result.error) throw result.error
  if (result.status !== 0) {
    throw new Error(`OpenCode ${arguments_.join(" ")} failed:\n${result.stdout}\n${result.stderr}`)
  }
  return result.stdout
}

try {
  opencode("plugin", "file:./clients/opencode", "--global", "--force")
  const config = JSON.parse(opencode("debug", "config"))
  const organization = config.provider?.organization
  if (!organization) throw new Error("official OpenCode did not load the organization provider")
  if (organization.options?.baseURL !== environment.ENTERPRISE_AI_GATEWAY_URL) {
    throw new Error("organization provider did not retain the configured gateway URL")
  }
  if (organization.models?.["coding-frontier"]?.tool_call !== true) {
    throw new Error("official OpenCode did not load the tool-capable fallback catalog")
  }

  const models = new Set(opencode("models", "organization").trim().split(/\r?\n/))
  for (const expected of ["organization/coding-frontier", "organization/general-fast", "organization/cheap-batch"]) {
    if (!models.has(expected)) throw new Error(`official OpenCode catalog is missing ${expected}`)
  }

  // The installer must have recorded only the intentionally isolated test config.
  const installed = await readFile(join(temporary, "config", "opencode", "opencode.jsonc"), "utf8")
  if (!installed.includes("file:./clients/opencode")) throw new Error("plugin was not recorded in isolated config")
  console.log("official OpenCode 1.18.26 loaded the packaged provider and fallback models")
} finally {
  await rm(temporary, { recursive: true, force: true })
}
