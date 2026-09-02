# Deployment guide

## Local development

Copy `.env.example` to `.env`, then run the following commands from the
repository root. Each Compose invocation supplies the root environment file
explicitly because the primary Compose file is under `gateway/`.

```sh
docker compose --env-file .env \
  -f gateway/compose.yaml -f gateway/compose.mock.yaml \
  --profile dev-idp --profile mock up --build --wait
docker compose --env-file .env \
  -f gateway/compose.yaml -f gateway/compose.mock.yaml \
  --profile mock --profile bootstrap run --rm bootstrap
python -m pip install -e ".[dev,integration]"
npm ci --omit=dev --workspace services/examples/typescript --include-workspace-root=false
pytest -m "integration and not real_provider" tests/e2e
```

The `dev-idp` Keycloak profile is strictly a local testing fixture and is not a
production identity provider. Its development realm listens only on localhost.
Seeded credentials are public test data and must never be reused:

- Keycloak administrator: `admin` / `development-only-admin`
- Human: `developer` / `development-only-password`
- Service client: `example-service` / `development-only-service-secret`

Stop the stack with:

```sh
docker compose --env-file .env \
  -f gateway/compose.yaml -f gateway/compose.mock.yaml down
```

Add `--volumes` only when you intentionally want to erase the local PostgreSQL
state.

## Production configuration

Use a Linux VM initially. Put the organization's reverse proxy/load balancer in
front of port 4000 and publish only HTTPS. Do not publish PostgreSQL. Configure:

- random PostgreSQL and LiteLLM admin secrets from the existing secret manager;
- an HTTPS issuer, audience, asymmetric algorithms, claim paths, and optional scopes;
- a production IdP client and user authorization policy that allows
  `offline_access` when OpenCode refresh-token rotation is required;
- a policy reviewed by identity, security, finance, and service owners;
- server-only provider keys and stable-alias target/base/key variables;
- OpenRouter account guardrails as the non-overridable privacy floor.

`OIDC_DISCOVERY_URL` and `OIDC_JWKS_URL` exist only to handle split-horizon/local
networking. In ordinary production, omit both and use discovery from the issuer.
`OIDC_ALLOW_HTTP` must remain false.

The checked-in upstream images are pinned by multi-architecture digest. CI must
build the custom gateway once, publish it to the approved registry, and record
the resulting digest in `GATEWAY_IMAGE`; a tag alone is not a production release
identifier. Verify LiteLLM's cosign signature against its official pinned public
key before building. The upstream image is mixed-license even though this
project exercises only the MIT extension path; see `THIRD_PARTY_NOTICES.md` and
obtain legal review.

In the production environment file, set `POSTGRES_PASSWORD` to the raw database
password used by the PostgreSQL container and set `DATABASE_URL` separately with
RFC 3986 percent-encoding for reserved username/password characters. Do not
construct a URI by interpolating an arbitrary raw password.

Before starting or rolling production gateways, make `catalog/generated/`
writable by the non-root UID used by the promoted image, then publish a fresh
approved catalog with that same image and start without an on-host build:

```sh
docker compose --env-file /run/secrets/enterprise-ai/runtime.env \
  -f gateway/compose.yaml -f infra/production/compose.yaml \
  --profile catalog-sync run --rm catalog-sync
docker compose --env-file /run/secrets/enterprise-ai/runtime.env \
  -f gateway/compose.yaml -f infra/production/compose.yaml up -d --wait
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
docker compose --env-file /run/secrets/enterprise-ai/runtime.env \
  -f gateway/compose.yaml \
  -f infra/production/compose.yaml --profile bootstrap run --rm bootstrap
```

It creates or updates team rows with model groups, monthly budgets, and RPM/TPM,
and clears team-level router overrides/model aliases. The master credential is a
management-only secret accepted on the bootstrap and shipped accounting-audit
route allowlist. The gateway rejects it everywhere else and substitutes a
non-secret identifier in downstream audit records.
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
3. Recreate the gateway; startup/auth classification derives the direct backend
   from the changed model adapter without a second alias list.
4. Run the same example client without changing its source.

Repoint every member of a configured fallback chain in the same rollout. The
gateway rejects a chain that mixes OpenRouter and direct adapters because a
fallback must not change the applicable provider-privacy contract.

The CI provider-switch overlay performs this sequence without paid inference:
it recreates the gateway with `general-fast` on LiteLLM's OpenRouter adapter,
runs the shipped Python client, recreates the gateway with a direct OpenAI-
compatible adapter, and runs the exact same client command again. Both paths use
the deterministic local backend; the opt-in staging test remains the proof for a
live OpenRouter-to-direct transition.
