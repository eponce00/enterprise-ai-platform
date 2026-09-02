# Production deployment scaffold

This directory is an operator blueprint for promoting the POC onto Linux virtual
machines. It is intentionally not a turnkey environment: identity, DNS, TLS,
secret-manager, database, backup, monitoring, and change-management details must
be supplied by the deploying organization.

The initial supported shape is Docker Compose on a Linux VM. Kubernetes, Helm,
an ingress controller, and an operator are explicitly out of scope. If scale or
organizational standards later justify Kubernetes, preserve the same container,
environment, health, migration, and policy contracts rather than making it a
prerequisite for the pilot.

## Deployment boundary

The checked-in `gateway/compose.yaml` is the single-node baseline:

```text
Internet or corporate network
        |
        v
managed load balancer or NGINX :443
        |
        v
LiteLLM gateway 127.0.0.1:4000
        |
        v
PostgreSQL on an un-published container network
```

Keep the gateway's host port on loopback. Publish only TCP 443 from the reverse
proxy, and restrict SSH and monitoring ports to administrative networks. Never
publish PostgreSQL. The development Keycloak profile is not part of production;
production uses the organization's external OIDC provider over HTTPS.

For the pilot, a co-located PostgreSQL container is acceptable when its failure
and recovery characteristics match the agreed service level. It is not HA. A
later HA deployment should place two or more identical gateway replicas behind a
load balancer and use managed or independently operated HA PostgreSQL with point-
in-time recovery.

The baseline Compose file constructs `DATABASE_URL` for its `postgres` service.
Moving to external PostgreSQL therefore requires an organization-owned Compose
override or service definition that replaces that environment value and removes
the local database dependency; setting `DATABASE_URL` beside the current file is
not sufficient. Validate the fully merged deployment with `config --quiet`.

## Release inputs

Promote immutable, reviewed inputs together:

- the gateway image pinned by digest;
- the exact LiteLLM version embedded in that image;
- `gateway/litellm/config.yaml` and the reviewed policy artifact;
- a catalog artifact no older than the configured maximum age;
- this repository revision and a release/change ticket;
- a database migration and rollback decision for that exact version.

Build once and promote the same digest through test, staging, and production.
Do not rebuild a tag during promotion and do not deploy `latest` or
`main-latest`.

## Reverse proxy and TLS

`nginx.conf.example` demonstrates the important API behavior. Adapt it to the
organization's normal load balancer or proxy rather than running a second proxy
when one already exists.

Required controls:

- terminate TLS with an automatically renewed certificate from the corporate PKI
  or an approved ACME issuer;
- redirect cleartext HTTP to HTTPS, or do not listen on HTTP at all;
- allow TLS 1.2 or newer according to organizational policy;
- enable HSTS only after the HTTPS hostname and renewal path are proven;
- preserve long-lived SSE responses by disabling response buffering and setting
  an appropriately long idle timeout;
- overwrite forwarding headers at the trusted edge; do not trust client-supplied
  identity headers;
- cap request-body size and connection/header timeouts without imposing a short
  timeout on legitimate model streams;
- keep health and Prometheus endpoints private;
- ensure access logs exclude `Authorization`, cookies, request/response bodies,
  prompts, completions, and provider credentials.

Test certificate renewal and a graceful proxy reload before launch. Alert well
before certificate expiry. If a load balancer and host proxy are chained, define
the trusted-proxy hops explicitly so source IP data cannot be spoofed.

## Secret injection

`runtime.env.example` is an inventory, not a secret file. A secret-manager agent
should render the real file outside the repository, for example at
`/run/secrets/enterprise-ai/runtime.env`, owned by root with mode `0600`. A tmpfs
location is preferred. Populate it without putting values in shell history,
CI logs, image layers, Compose YAML, source control, or a systemd unit.

The current application reads ordinary environment variables; it does not
implement a generic `_FILE` convention. Passing a protected environment file to
Compose is therefore the pilot integration point:

```sh
sudo docker compose \
  --env-file /run/secrets/enterprise-ai/runtime.env \
  -f gateway/compose.yaml config --quiet

sudo docker compose \
  --env-file /run/secrets/enterprise-ai/runtime.env \
  -f gateway/compose.yaml up -d --wait
```

Do not run `docker compose config` without `--quiet` in CI or support transcripts,
because resolved configuration can contain secrets. Docker administrators can
inspect container environments; treat control of the Docker daemon as root-level
access.

At minimum, manage and rotate these values independently:

- PostgreSQL credentials and, when externalized, the complete `DATABASE_URL`;
- `LITELLM_MASTER_KEY`, reserved for bootstrap/administration and never issued to
  users or services;
- OpenRouter and direct-provider credentials, held only by the gateway;
- any confidential OIDC client credential used outside the gateway;
- proxy certificate private keys.

OIDC issuer, audience, algorithms, and policy paths are configuration rather than
secrets, but promote them through the same reviewed release process. Production
must leave `OIDC_ALLOW_HTTP=false`. Test rotation first in staging, support a
short overlap where the provider allows it, revoke the old value, and verify both
inference and accounting after rotation.

## Deployment and migration runbook

Use a maintenance window until the exact release has demonstrated online,
backward-compatible migrations. For each release:

1. Review LiteLLM release notes, license boundaries, database changes, and the
   pinned image signature/digest.
2. Run unit, mock E2E, catalog-outage, and real-provider tests in staging with the
   production policy shape.
3. Take a recoverable database backup or point-in-time marker and verify that the
   most recent scheduled backup can be restored.
4. Exercise the upgrade against a restored copy of production data. Record the
   runtime, locks, disk growth, and validation queries.
5. Drain inference traffic if the schema change is not proven compatible. Allow
   existing SSE requests to finish before stopping a replica.
6. Ensure exactly one actor performs schema migration. If the pinned LiteLLM
   image migrates automatically at startup, start one isolated instance first;
   do not race every replica against the schema.
7. Start the gateway, run the policy bootstrap once, then verify OIDC, model
   listing, a normal completion, streaming, tool calling, usage/cost attribution,
   budgets, and provider privacy constraints.
8. Admit a canary to traffic, watch error, latency, spend, and database signals,
   then roll the remaining replicas one at a time.

Do not invent an ad-hoc Prisma command from a different LiteLLM version. Use the
migration mechanism documented and tested for the pinned image. Treat schema
rollback separately from image rollback: an older image is safe only when the
new schema is backward compatible. Otherwise restore the pre-migration backup to
a new database and switch over after validation. Never use `compose down
--volumes` as a rollback procedure.

## Database backup and restore

Set explicit recovery objectives before launch. The database holds accounting,
budget, identity/team, and gateway state; losing it can affect both auditability
and enforcement.

Preferred production controls are:

- managed PostgreSQL or an independently monitored PostgreSQL service;
- encrypted automated backups plus continuous WAL/PITR where the required RPO
  cannot be met by periodic logical dumps;
- off-host, access-controlled backup storage in a separate failure domain;
- retention and deletion aligned with security, finance, and audit policy;
- checksums, backup-job alerts, and a recorded database/server version;
- a scheduled restore drill, not merely a successful backup-job status.

For a small self-managed pilot, use supported PostgreSQL tooling such as
`pg_dump`/`pg_restore` or a physical backup system. Supply credentials through a
root-owned PostgreSQL service/pass file or secret agent, not command-line
passwords. Encrypt the artifact before it leaves the database host.

A restore drill must target an isolated, empty database—never the live database:

1. Verify the intended backup ID, checksum, encryption key access, PostgreSQL
   version, and exact restore target.
2. Restore into an isolated network and a newly created database.
3. Start one gateway instance with provider egress disabled or test credentials.
4. Validate schema/version, team and budget rows, usage totals, policy bootstrap,
   authentication, and representative API reads.
5. Record achieved RPO/RTO and securely destroy the temporary copy according to
   the data-handling policy.

The disaster-recovery order is database, secret injection, gateway, policy
bootstrap, private smoke tests, then traffic. Back up policy/configuration as
versioned artifacts, but keep provider keys and generated runtime secret files
out of repository and backup bundles unless the approved secret manager itself
is being backed up.

## High availability and scaling

The baseline Compose file has a fixed loopback port and one PostgreSQL container,
so `docker compose up --scale gateway=N` is not a valid HA design. Scale by
running one or more gateway instances per VM on distinct private listeners, or
one instance on each of multiple VMs, behind the organization's load balancer.

Before adding replicas:

- use the same immutable image, policy, catalog, OIDC configuration, provider
  credentials, and master key on every replica;
- move PostgreSQL outside the single VM and configure connection limits/pooling;
- identify LiteLLM features that rely on process-local state. Configure a
  supported shared coordination/cache service where required for consistent
  cross-replica rate limits, cooldowns, or caches, and test the chosen behavior;
- make bootstrap and catalog publication single-writer/idempotent operations;
- use readiness—not mere process liveness—to admit a replica;
- validate that load-balancer draining preserves long-running SSE requests;
- load-test provider limits, database connections, file descriptors, memory, and
  egress before setting replica autoscaling or capacity thresholds.

Prefer availability-zone separation for gateway replicas and the database. Do
not use sticky sessions as a substitute for shared state. Provider outages and
quota exhaustion require configured routing/fallback behavior even when the
gateway itself is healthy.

## Health, telemetry, and alerting

The gateway exposes LiteLLM liveness at `/health/liveliness`, and the checked-in
configuration enables Prometheus success/failure callbacks. Scrape metrics over
the private network only. Define a separate readiness check that covers the
dependencies required to accept traffic, especially database connectivity and
loaded routing/policy configuration.

Collect structured logs and metrics for:

- request rate, concurrent/streaming requests, duration percentiles, and status;
- authentication failures and policy denials, without logging bearer tokens;
- model, provider/backend, fallback, retry, cooldown, and rate-limit outcomes;
- input/output/cached tokens and estimated/actual cost by approved identity/team
  dimensions;
- budget exhaustion and discrepancies between successful requests and accounting;
- PostgreSQL availability, connections, locks, latency, storage, replication,
  and backup age;
- catalog age/synchronization failures, certificate expiry, host/container
  saturation, and provider reachability.

Never use prompt, completion, authorization, or other high-cardinality sensitive
content as metric labels. Establish retention and access controls for identity
and spend telemetry. Propagate or generate a request ID at the trusted edge and
include it in gateway logs so incidents can be correlated without content logs.

At minimum, page on sustained availability failures, inability to authenticate,
database unavailability, failed backups, and uncontrolled spend. Ticket or warn
on latency/error-budget burn, repeated provider fallback, catalog staleness,
certificate age, approaching budgets, and resource saturation. Route a synthetic
OIDC-backed request through the public endpoint on a schedule using a dedicated,
low-budget service identity.

## Production readiness checklist

- DNS and TLS renewal/reload tested; only HTTPS is published.
- Development IdP/profile and all seeded development credentials are absent.
- Secrets originate in the approved manager and rotation is rehearsed.
- Provider account guardrails match the server-enforced privacy policy.
- Exact images/digests, configuration, policy, and catalog are recorded.
- Migration and image rollback decisions are written for this release.
- Encrypted off-host backup and isolated restore drill meet RPO/RTO.
- Liveness, readiness, metrics, logs, dashboards, and alerts are operational.
- Real OIDC and provider smoke tests pass with short-lived credentials.
- On-call ownership, provider-spend escalation, and incident runbooks are assigned.
- The pilot's single-node limitations—or the HA topology—are explicit and accepted.
