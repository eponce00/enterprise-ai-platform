# Security and privacy

## Trust boundaries

- Client machines hold only short-lived organizational OAuth tokens.
- LiteLLM holds provider keys and the admin key server-side.
- PostgreSQL is private and stores policy/accounting metadata.
- OpenRouter and its selected provider receive inference content.
- The external IdP remains the authority for authentication and MFA.

## Controls implemented

- Exact issuer/audience, asymmetric JWT signatures, time claims, optional scopes,
  discovery-issuer pinning, cached JWKS, and rotation refresh.
- OIDC environment settings and the identity policy are validated before the
  gateway process starts serving or reports healthy; unresolved database and
  master-key references also stop startup.
- PKCE, random state, loopback-only callback, short callback timeout, and exact
  gateway origin/path checking before bearer attachment.
- Unknown identity/model mapping denial, server-side common checks, and explicit
  denial when team state is unreadable after the five-second last-known-good
  management-cache window. Spend-counter verification/reservation separately
  uses fail-closed budget enforcement.
- OpenRouter ZDR/data-collection/provider restrictions overwrite weaker caller
  values; tool calls require supporting provider endpoints. Reviewed caller
  controls can only narrow the OpenRouter provider set or strengthen privacy;
  unknown provider controls are denied. Direct routes reject OpenRouter-specific
  controls. Clients cannot override server-managed model or fallback routing,
  including through nested `extra_body` fields, and configured fallback chains
  must stay within one backend privacy class. Every reachable fallback is checked
  against the caller's model grant before upstream dispatch. Bootstrap also
  clears database team-level router overrides and aliases; an empty DB-object
  allowlist keeps deployments/global routing source-controlled even if stale DB
  state attempts to re-enable model storage. Management-plane drift must alert.
- The rendered LiteLLM startup document is a fail-closed policy boundary. Only
  reviewed top-level sections, security settings, model-entry fields, deployment
  parameters, and finite non-negative catalog costs are accepted. Environment
  rewrites, alternate local/bucket config sources, credential indirection,
  automatic routers, model aliases, and other unreviewed extension points stop
  the gateway before it serves traffic.
- Provider/admin secrets are absent from images, source, client config, and logs.
- The master credential is accepted only on an explicit bootstrap/accounting
  route allowlist, is replaced with a non-secret audit alias after comparison,
  and cannot authorize any other management or data-plane route. OIDC
  credentials are accepted only on the exact model-list and chat-completion
  routes published by this release.
- Message logging and prompt storage in spend logs are disabled; API key
  information is redacted, and clients cannot disable callbacks with `no-log`.
  The integration suite checks chat-completion spend detail for prompt and
  response leakage. Review and regression-test retention separately before
  enabling another API family such as embeddings or images.

## Data retention

- Clients may retain local conversation history according to their own settings;
  the gateway cannot erase or govern a client's local files.
- LiteLLM and PostgreSQL retain identity, model, token, cost, latency, status,
  and request metadata. They do not retain prompt or completion bodies in the
  default configuration.
- Prometheus receives gateway metrics, not prompt or completion bodies. Any
  future logging or tracing integration needs its own field and retention review.
- OpenRouter and the selected upstream endpoint necessarily receive inference
  content. Account guardrails and request-level ZDR/data-collection controls are
  applied, but endpoint-specific terms, jurisdiction, and retention still need
  organizational approval.

There is intentionally no environment-variable shortcut for content logging.
Enabling it for debugging requires a reviewed configuration change, a bounded
retention period, and removal or redaction of sensitive test data.

Use OpenRouter account guardrails as the policy floor because request controls
should only narrow—not define—the organization's global policy. ZDR is stronger
than no-training. Underlying endpoint terms and jurisdictions still require
review. Strict workloads should avoid response caching and maintain an explicit
approved provider list.

## Explicit non-goals

This POC does not attempt device binding, mTLS/TPM attestation, MDM, DLP, network
blocking, or prevention of personal AI accounts. It also does not make a user's
bearer token impossible for that user to inspect. The enforced boundary is that
the plugin never receives provider credentials and requests using
organization-managed provider credentials pass through gateway policy. This
does not prevent a device owner from using separate clients, credentials, or
endpoints.

Before production, threat-model ingress/header trust, rotate all example secrets,
verify backup encryption, set log retention, test key compromise/revocation,
review provider terms, scan images/dependencies, and perform an IdP-specific
penetration test.
