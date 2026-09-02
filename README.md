# Enterprise AI Platform

An open-source-first, self-hosted AI access layer that keeps clients and model
providers replaceable. Official OpenCode and ordinary OpenAI-compatible services
authenticate with short-lived organizational OIDC tokens; LiteLLM validates
identity, applies policy, attributes usage, and routes to OpenRouter, direct
providers, or future local inference.

```text
OpenCode / services -> OIDC JWT -> LiteLLM -> OpenRouter / direct / local
```

Provider credentials exist only on the gateway. Prompt and completion content
logging is off by default. No OpenCode or LiteLLM fork, Enterprise feature, or
custom admin portal is required.

## Quick start (local, no paid inference)

Prerequisites: Docker with Compose, Python 3.10+, Node.js 22.22.2+.

```sh
cp .env.example .env
docker compose -f gateway/compose.yaml -f gateway/compose.mock.yaml \
  --profile dev-idp --profile mock up --build --wait -d
docker compose -f gateway/compose.yaml -f gateway/compose.mock.yaml \
  --profile mock --profile bootstrap run --rm bootstrap
python -m pip install -e ".[dev,integration]"
npm ci --omit=dev --workspace services/examples/typescript --include-workspace-root=false
pytest -m "integration and not real_provider" tests/e2e
```

The local realm is development-only. Its seeded service identity can obtain a
token and call the mock-backed `general-fast` alias. See
[`docs/deployment.md`](docs/deployment.md) for the exact commands and health
checks.

## Development checks

```sh
python -m pip install -e ".[dev]"
pytest
ruff check .
npm ci
npm test
npm run typecheck
npm run test:opencode-smoke
docker compose -f gateway/compose.yaml config --quiet
```

## Repository map

- `gateway/`: pinned LiteLLM image, Compose stack, OIDC auth, and policy adapter.
- `clients/opencode/`: installable OpenCode plugin with PKCE and token refresh.
- `catalog/`: OpenRouter discovery, filtering, aliases, and generated artifacts.
- `services/examples/`: Python, TypeScript, and curl service clients.
- `tests/e2e/`: free local mock-provider path and opt-in real-provider checks.
- `docs/`: user, application developer, administrator, security, and ADR docs.

## Production boundary

This is an implementation and deployment-scaffold POC, not a claim that example
policy is production policy.
Before rollout, choose approved aliases/providers, set budgets, configure HTTPS,
use a secrets manager, test the real IdP, and run the live-provider validation.

Apache-2.0. See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
