# Deployment guide

## Local development

Copy `.env.example` to `.env`, then start the free mock path:

```sh
docker compose -f gateway/compose.yaml -f gateway/compose.mock.yaml \
  --profile dev-idp --profile mock up --build --wait
docker compose -f gateway/compose.yaml -f gateway/compose.mock.yaml \
  --profile mock --profile bootstrap run --rm bootstrap
python -m pip install -e ".[dev,integration]"
npm ci --omit=dev --workspace services/examples/typescript --include-workspace-root=false
pytest -m "integration and not real_provider" tests/e2e
```

The development realm listens only on localhost. Seeded credentials are public
test data and must never be reused:

- Keycloak administrator: `admin` / `development-only-admin`
- Human: `developer` / `development-only-password`
- Service client: `example-service` / `development-only-service-secret`

Stop the stack with `docker compose ... down`. Add `--volumes` only when you
intentionally want to erase the local PostgreSQL state.

## Production configuration

Use a Linux VM initially. Put the organization's reverse proxy/load balancer in
front of port 4000 and publish only HTTPS. Do not publish PostgreSQL. Configure:

- random PostgreSQL and LiteLLM admin secrets from the existing secret manager;
- an HTTPS issuer, audience, asymmetric algorithms, claim paths, and optional scopes;
- a policy reviewed by identity, security, finance, and service owners;
- server-only provider keys and stable-alias target/base/key variables;
- OpenRouter account guardrails as the non-overridable privacy floor.

`OIDC_DISCOVERY_URL` and `OIDC_JWKS_URL` exist only to handle split-horizon/local
networking. In ordinary production, omit both and use discovery from the issuer.
`OIDC_ALLOW_HTTP` must remain false.

The checked-in images are pinned by multi-architecture digest. Verify LiteLLM's
cosign signature against its official pinned public key before promotion. The
upstream image is mixed-license even though this project exercises only the MIT
extension path; see `THIRD_PARTY_NOTICES.md` and obtain legal review.

Before starting or rolling production gateways, publish a fresh approved
catalog on the host:

```sh
python -m catalog.sync --policy catalog/catalog-policy.yaml
docker compose -f gateway/compose.yaml up -d --build --wait
```

Compose mounts `catalog/generated/` read-only. Startup validates the artifact
timestamp and schema, adds explicit raw-model routes with catalog pricing, and
leaves the source file untouched. Set `APPROVED_CATALOG_MAX_AGE_SECONDS` to the
approved freshness window. Missing or rejected artifacts fall back to aliases
only and are visible in startup logs; raw model invocation then fails closed. A
newly published artifact is consumed on the next gateway restart/rollout.

## Policy reconciliation

Run the one-shot bootstrap after policy changes. It uses the documented LiteLLM
management API, not database internals:

```sh
docker compose -f gateway/compose.yaml --profile bootstrap run --rm bootstrap
```

It creates or updates team rows with model groups, monthly budgets, and RPM/TPM.
Custom auth deliberately does not self-call LiteLLM to JIT-provision users: that
creates an auth recursion/availability coupling. The shipped POC enforces team
budgets. If per-user ceilings inside a team are required, reconcile LiteLLM team
members and their member budgets through supported management APIs, and add a
strict user-presence integration test. Do not assume a personal `/user/new`
budget applies to team-associated requests.

## Real OpenRouter validation

Set the server-side `OPENROUTER_API_KEY` and alias keys, use a token mapped to a
team whose authorization policy permits the selected explicit `openrouter/<id>`
routes, and set five current model families. Every supplied ID must be present in
the fresh approved-catalog artifact:

```sh
RUN_REAL_PROVIDER_TESTS=1 \
REAL_OIDC_TOKEN='<short-lived token>' \
REAL_MODEL_IDS='openai/...,anthropic/...,google/...,deepseek/...,mistralai/...' \
pytest -m real_provider tests/e2e/test_real_openrouter.py
```

This test incurs cost and is never run in ordinary CI. Select non-expired IDs
from the synchronized catalog at validation time.

## Provider-independence acceptance test

1. Call `general-fast` with its OpenRouter model/base/key variables.
2. Change only `GENERAL_FAST_MODEL`, `GENERAL_FAST_API_BASE`, and
   `GENERAL_FAST_API_KEY` to a direct OpenAI-compatible backend.
3. Remove `general-fast` from `OPENROUTER_POLICY_MODELS` and recreate the gateway.
4. Run the same example client without changing its source.

The CI provider-switch overlay performs this sequence without paid inference:
it recreates the gateway with `general-fast` on LiteLLM's OpenRouter adapter,
runs the shipped Python client, recreates the gateway with a direct OpenAI-
compatible adapter, and runs the exact same client command again. Both paths use
the deterministic local backend; the opt-in staging test remains the proof for a
live OpenRouter-to-direct transition.
