# Security policy

## Reporting a vulnerability

Please do not disclose suspected vulnerabilities in a public issue or pull
request. Use the repository's **Security** tab and choose **Report a
vulnerability** to open a private security advisory.

Include the affected component, reproduction steps, expected impact, and any
suggested mitigation. Avoid including real credentials, access tokens, prompts,
or organizational data in the report.

## Supported versions

This project is currently a proof of concept. Security fixes are applied to the
latest revision on the default branch; no older release line is maintained yet.

## Deployment responsibility

Example identities, secrets, budgets, and policies are development fixtures.
Operators must replace them, configure HTTPS and an approved IdP, review
provider retention terms, and complete the production checklist before rollout.
See [`docs/security.md`](docs/security.md) and
[`infra/production/README.md`](infra/production/README.md).
