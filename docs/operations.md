# Operations

## Health and observability

Probe LiteLLM `/health/liveliness`; use a separate readiness check that verifies
database/routing dependencies before adding a replica to traffic. Prometheus
callbacks expose request count, latency, errors, tokens, spend, models, users,
teams, and provider dimensions. Connect those metrics to the existing Grafana/
alerting stack; do not create a bespoke dashboard application.

Alert on authentication failure spikes, 429/5xx rates, provider fallback/cooldown,
catalog age, abnormal spend, budget exhaustion, database saturation, and token/
cost accounting gaps. Never add raw prompts or completions to metric labels.

The mock accounting test temporarily lowers and then restores the `automation`
team budget to prove database-backed denial. It refuses this mutation on a
non-loopback gateway unless `ALLOW_E2E_POLICY_MUTATION=1` is explicitly set.
Run it only against an isolated test environment and never concurrently with
other policy reconciliation.

## Catalog operation

Run `python -m catalog.sync` on a schedule and publish the atomically generated
artifact through the organization's normal configuration channel. An outage may
use only a cache newer than the configured maximum; otherwise synchronization
fails closed and leaves the last file untouched.

Gate publication on the catalog tests, make the artifact readable by the
gateway container identity, and mount the generated directory read-only. Roll
gateway replicas after publication because explicit routes and per-token prices
are rendered only at process startup. Confirm the startup log reports the
expected approved-model count and alert when it reports a stale/invalid fallback
or when the running catalog generation is older than policy allows.

## Backups and recovery

Use PostgreSQL-native encrypted backups, daily restore verification, and a
retention policy aligned with finance/security requirements. Back up configuration
and policy in version control; do not back up provider secrets into the repo.
Recovery order is PostgreSQL, secret injection, gateway, policy reconcile, smoke
test, then traffic.

## Upgrades

Dependabot opens reviewable dependency PRs. Never auto-deploy new LiteLLM or
OpenCode major versions. For each upgrade:

1. Review license and Enterprise boundary changes.
2. Verify image signature/digest and release notes.
3. Run unit, plugin, mock Compose, and catalog-outage tests.
4. Run opt-in real-provider family validation in staging.
5. Canary with spend/error alerts, then promote or roll back the pinned digest.

OpenCode plugin failures prevent client rollout. Database migrations require a
tested backup/restore and version-specific rollback plan.
