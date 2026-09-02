# ADR-001: Why LiteLLM is the stable internal API

## Status

Accepted.

## Context

The platform must serve OpenCode, services, pipelines, and future clients while allowing inference providers to change independently. Exposing a provider-specific API or credential to clients would couple both sides and fragment authentication, policy, and accounting.

## Decision

Use LiteLLM OSS as the stable company-facing, OpenAI-compatible API. All clients authenticate to LiteLLM, which performs identity attribution, policy enforcement, accounting, and provider routing. Provider credentials remain server-side, and OpenCode-specific behavior stays outside the gateway except for application attribution.

## Consequences

- Clients share one endpoint and can change without redesigning provider integration.
- Providers can change behind LiteLLM without distributing new credentials.
- Gateway availability, compatibility, upgrades, and policy correctness become platform responsibilities.
- The approved LiteLLM version and integration tests must be pinned together.
