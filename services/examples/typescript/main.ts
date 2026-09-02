import OpenAI from "openai"

interface TokenResponse {
  access_token: string
}

function requiredEnvironment(name: string): string {
  const value = process.env[name]
  if (!value) throw new Error(`${name} is required`)
  return value
}

const tokenResponse = await fetch(requiredEnvironment("OIDC_TOKEN_URL"), {
  method: "POST",
  headers: { "content-type": "application/x-www-form-urlencoded" },
  body: new URLSearchParams({
    grant_type: "client_credentials",
    client_id: requiredEnvironment("OIDC_CLIENT_ID"),
    client_secret: requiredEnvironment("OIDC_CLIENT_SECRET"),
    scope: process.env.OIDC_SCOPE ?? "",
  }),
})
if (!tokenResponse.ok) throw new Error(`OIDC token request failed: ${tokenResponse.status}`)
const { access_token: accessToken } = await tokenResponse.json() as TokenResponse
if (!accessToken) throw new Error("OIDC token response has no access_token")

const client = new OpenAI({
  baseURL: requiredEnvironment("GATEWAY_URL"),
  apiKey: accessToken,
})
const response = await client.chat.completions.create({
  model: process.env.MODEL ?? "general-fast",
  messages: [{ role: "user", content: "Summarize the deployment status in one sentence." }],
})
console.log(`model=${response.model} content=${response.choices[0].message.content}`)
