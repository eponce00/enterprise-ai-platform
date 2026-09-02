# Application and service integration

Internal services obtain a short-lived OIDC access token and call the gateway as
an ordinary OpenAI-compatible API. They never receive an OpenRouter/vendor key.

```python
client = OpenAI(base_url=GATEWAY_URL, api_key=short_lived_service_token)
result = client.chat.completions.create(
    model="general-fast",
    messages=[{"role": "user", "content": "..."}],
)
```

Complete Python, TypeScript, and curl examples live in `services/examples`.
Production code should cache a token until shortly before expiration, single-
flight concurrent refreshes, use bounded retries with jitter, set timeouts, and
propagate its request/correlation ID.

Use logical aliases for production code. Raw `openrouter/...` names are for
authorized evaluation/experimentation. A 401 means obtain a fresh token; 403
means identity/model policy denial; 429 means honor `Retry-After`; 5xx may be
retried within the service's idempotency and latency budget.

Usage is available through LiteLLM spend logs/metrics and includes issuer-scoped
user/service ID, team, logical and backend model, provider, token counts, cost,
latency, request ID, timestamp, and status. The trusted identity kind/client ID
is persisted as the key alias (for example, `service/example-service`) so
applications remain distinguishable without custom Enterprise metadata.
Prompt/completion content is intentionally absent by default.
