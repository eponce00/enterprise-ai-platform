# Architecture

## Context and invariants

The platform is an identity-aware inference control plane, not an OpenCode
backend. OpenCode is the first human client; CI, agents, batch jobs, and internal
applications use the same OpenAI-compatible endpoint.

```text
                      external OIDC provider
                         /             \
                human + PKCE      service/workload JWT
                       |                 |
        official OpenCode + plugin       |
                       \                 /
                        v               v
                     LiteLLM OSS gateway
                  auth | policy | accounting
                       /        |         \
                OpenRouter   direct API   local OpenAI API
```

The invariants are:

1. Clients depend on the LiteLLM-compatible contract, not OpenRouter.
2. Provider credentials never leave the server trust boundary.
3. Every accepted request has an issuer-scoped subject and one deterministic team.
4. Human and service identities use short-lived, locally validated JWTs.
5. Model access, rates, budgets, and privacy constraints are server policy.
6. Prompt/completion content is not logged by default.
7. No upstream fork or paid extension is needed for the demonstrated path.

## Request lifecycle

1. The client gets a JWT from the external IdP. Humans use Authorization Code +
   PKCE; services use client credentials or workload identity.
2. The LiteLLM custom-auth hook obtains the bearer value and validates its
   signature, algorithm, issuer, audience, time claims, and configured scopes.
3. Claims become a vendor-neutral identity. Ordered mappings choose exactly one
   team and privacy profile; no match is a denial.
4. The hook rejects a disallowed logical/raw model and returns LiteLLM
   `UserAPIKeyAuth` identity, model, team, and rate fields.
5. LiteLLM common checks load the reconciled team row from PostgreSQL and
   enforce persistent team budget, rate, and model rules. The gateway opts into
   hard, fail-closed team-budget verification and rejects requests when team
   policy cannot be verified.
6. The pre-call policy hook overwrites OpenRouter routing fields with at least
   the organization's ZDR/data-collection/provider restrictions.
7. LiteLLM resolves the alias or approved explicit raw route and calls the selected provider with a
   server-side credential.
8. Token counts, cost, latency, issuer-scoped identity ID, team, model, provider,
   request ID, and status are available to spend logs/metrics. The trusted
   identity kind/client ID is encoded in LiteLLM's persisted key alias for
   application attribution. Request content stays out of logs unless an
   operator explicitly opts in.

## Stable aliases and raw models

Stable aliases (`general-fast`, `coding-frontier`, and similar) are explicit
LiteLLM model groups. Production applications use them. Advanced users may use
authorized `openrouter/<author>/<model>` names. A fresh approved-catalog artifact
is rendered into explicit, priced LiteLLM routes at gateway startup. There is no
unrestricted wildcard route: an absent, stale, or rejected catalog leaves only
stable aliases, preventing guessed model IDs from bypassing catalog policy.
Catalog discovery, route exposure, and team authorization remain separate layers.

Changing an alias means changing its model, API base, and secret variables on
the gateway. The client source and authentication do not change. Direct/local
providers must also be removed from `OPENROUTER_POLICY_MODELS`, so OpenRouter-
specific routing parameters are not sent to them.

## Deployment evolution

The POC baseline is one gateway and one PostgreSQL database behind
operator-supplied external HTTPS ingress. Scale-out keeps DNS/API stable: put
multiple stateless LiteLLM replicas behind a load balancer, use managed
PostgreSQL, share any distributed rate-limit cache, and connect existing
metrics/logging. Kubernetes is one option, not a contract.
