# ADR-006: Policy and budget mapping

## Status

Accepted.

## Context

The gateway must enforce model access, privacy constraints, rates, and spend consistently for rotating human and service tokens. JWT strings are not durable accounting keys, and LiteLLM's cross-request team checks require persisted policy records.

## Decision

Map trusted OIDC groups, roles, and service client IDs through ordered configuration to exactly one LiteLLM team and privacy profile; unknown identities fail closed. Attribute requests to stable issuer-scoped user IDs and reconcile teams into PostgreSQL without issuing virtual keys. Enable LiteLLM common and post-custom-auth checks for database-backed team budgets and allowlists, and apply privacy routing in a gateway pre-call hook. Pinned LiteLLM 1.99.0 does not copy DB-team RPM/TPM fields onto a centralized custom-auth result, so return the same source-controlled rates reconciled by bootstrap for its team and per-identity limiter dimensions. The custom-auth token deliberately carries no fallback team model grant or budget, so an unreadable team row still denies access. Run bootstrap and any optional user reconciliation out of band from request authentication.

## Consequences

- Team limits and user attribution survive token refreshes, restarts, and eventual gateway replication.
- Rate changes are source-controlled rollouts followed by bootstrap; unsupported
  ad-hoc DB-only rate edits are not an enforcement mechanism on this custom-auth
  path.
- Per-user budgets within a team require an explicit supported reconciler/team-member budget path; the shipped POC demonstrates the team budget boundary.
- PostgreSQL availability, migrations, backups, cost metadata, and reconciliation are operational requirements.
- Missing or stale policy records must deny access rather than expand it.
- Production mappings, budgets, rates, and provider restrictions require organizational approval and periodic review.
