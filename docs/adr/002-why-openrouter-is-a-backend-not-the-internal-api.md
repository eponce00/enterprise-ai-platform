# ADR-002: Why OpenRouter is a backend, not the internal API

## Status

Accepted.

## Context

OpenRouter provides broad initial model coverage and convenient provider routing. Making its API, credentials, or model identifiers the client contract would replace vendor lock-in with marketplace lock-in and obstruct later use of direct or local inference.

## Decision

Use OpenRouter as the initial default backend behind LiteLLM. Clients never receive its credential or call it directly. Stable aliases may target OpenRouter, a direct vendor API, or a local OpenAI-compatible service, and OpenRouter-specific privacy or routing parameters apply only to OpenRouter-backed routes.

## Consequences

- The pilot gains broad model choice with one server-side integration.
- OpenRouter outages and credentials remain contained at the gateway.
- Backend changes still require gateway configuration and compatibility testing, but no client source or credential changes.
- Acceptance tests must prove that an alias can move to a non-OpenRouter backend transparently.
