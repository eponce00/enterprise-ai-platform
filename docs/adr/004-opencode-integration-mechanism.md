# ADR-004: OpenCode integration mechanism

## Status

Accepted.

## Context

OpenCode is the first human client, but its APIs evolve and the platform must not depend on a custom executable. The tested upstream release provides stable authentication, configuration, provider-model, and custom-request hooks.

## Decision

Use official OpenCode 1.18.26 with a separately packaged organization plugin built on its V1 `auth`, `config`, `provider.models`, and custom `fetch` hooks. The plugin performs PKCE login on a loopback callback, refreshes tokens with single-flight coordination, injects the current bearer token, configures LiteLLM, and exposes approved models. A loopback forwarding proxy remains a fallback only if a future supported version cannot inject refreshed credentials; an OpenCode fork is the last resort.

## Consequences

- Users retain the official OpenCode distribution and normal plugin installation flow.
- Compatibility tests for login, refresh, discovery, streaming, and tools must gate upgrades.
- Managed provider settings reduce accidental bypass but are not a laptop security boundary.
- OpenCode-specific code remains isolated in the plugin package.
