# ADR-005: Model catalog strategy

## Status

Accepted.

## Context

OpenRouter's model inventory changes frequently, while production applications need durable names when models are renamed, removed, repriced, or moved between backends. Hard-coding the upstream catalog becomes stale, but exposing every discovered entry would bypass organizational policy.

## Decision

Synchronize OpenRouter metadata into a cached, policy-filtered discovery catalog. Expose explicit logical aliases such as `general-fast` and `coding-frontier` for production applications. At startup, render every approved raw model as an explicit LiteLLM route. Do not expose an unrestricted OpenRouter wildcard route: otherwise a caller could guess a model rejected by catalog price, capability, or privacy policy. Discovery never grants team authorization, and catalog outages fall back to the last known approved cache or stable aliases.

## Consequences

- Users can discover current approved models without repository releases.
- Alias targets can change model or backend without application source changes.
- Catalog filtering, removal, deprecation, stale-cache, and outage behavior require tests and monitoring.
- Alias changes are controlled releases that require capability, cost, and privacy review.
