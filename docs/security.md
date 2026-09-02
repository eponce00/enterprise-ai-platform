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
- PKCE, random state, loopback-only callback, short callback timeout, and exact
  gateway origin/path checking before bearer attachment.
- Unknown identity/model mapping denial, server-side common checks, and explicit
  fail-closed team-policy/team-budget verification.
- OpenRouter ZDR/data-collection/provider restrictions overwrite weaker caller
  values; tool calls require supporting provider endpoints.
- Provider/admin secrets are absent from images, source, client config, and logs.
- Message logging and prompt storage in spend logs are disabled; API key
  information is redacted. The integration suite checks chat-completion spend
  detail for prompt and response leakage. Review and regression-test retention
  separately before enabling another API family such as embeddings or images.

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
bearer token impossible for that user to inspect. The guarantee is that a token
cannot be exchanged for provider credentials and all company-funded inference remains
behind policy.

Before production, threat-model ingress/header trust, rotate all example secrets,
verify backup encryption, set log retention, test key compromise/revocation,
review provider terms, scan images/dependencies, and perform an IdP-specific
penetration test.
