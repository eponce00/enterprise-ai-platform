# Third-party notices

This repository integrates with, but does not copy source code from, these
upstream projects:

- LiteLLM, MIT License, <https://github.com/BerriAI/litellm>
- OpenCode, MIT License, <https://github.com/anomalyco/opencode>
- Keycloak, Apache License 2.0, <https://github.com/keycloak/keycloak>
- PostgreSQL, PostgreSQL License, <https://www.postgresql.org/about/licence/>

Runtime and development dependencies retain their own licenses. Run
`python -m piplicenses` and `npm license-checker --production` in release CI
to produce a bill of materials for a distributed artifact.

No LiteLLM Enterprise or OpenCode Enterprise source is copied into this
repository. The official LiteLLM 1.99.0 container is a mixed-license upstream
distribution that includes proprietary modules even though this project uses
only its MIT custom-auth/common-check extension path and requires no enterprise
key. Organizations that prohibit proprietary bits in runtime images should
obtain legal review and build/test an OSS-only image from the MIT-licensed
source outside LiteLLM's `enterprise/` directory.
