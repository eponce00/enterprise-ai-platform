# ADR-007: Why upstream projects are extended rather than forked

## Status

Accepted.

## Context

Long-lived OpenCode or LiteLLM forks would absorb upstream security fixes, merge conflicts, release engineering, and license-boundary risk. Both approved upstream versions provide public extension points sufficient for the required client and gateway behavior.

## Decision

Use official, pinned upstream releases and extend them only through documented interfaces: an OpenCode plugin, LiteLLM custom authentication and callbacks, provider configuration, and external catalog/bootstrap tooling. Do not copy or unlock Enterprise-only code. Consider a fork only after a documented feasibility review proves that public extension mechanisms cannot preserve a required architecture invariant.

## Consequences

- Upstream fixes and releases remain easier to adopt.
- Integration code must tolerate public API evolution and is protected by compatibility CI.
- Some behavior requires maintained plugin, auth, catalog, and deployment glue rather than upstream modifications.
- Any future fork requires a new ADR covering scope, maintenance ownership, migration, and licensing.
