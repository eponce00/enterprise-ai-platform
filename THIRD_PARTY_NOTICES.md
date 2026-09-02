# Third-party notices

This repository integrates with, but does not copy source code from, these
upstream projects:

- LiteLLM, MIT License, <https://github.com/BerriAI/litellm>
- OpenCode, MIT License, <https://github.com/anomalyco/opencode>
- Keycloak, Apache License 2.0, <https://github.com/keycloak/keycloak>
- PostgreSQL, PostgreSQL License, <https://www.postgresql.org/about/licence/>

Runtime and development dependencies retain their own licenses. Install the
application into an isolated release environment, then use separately isolated,
pinned CycloneDX tools to generate machine-readable inventories:

```sh
npm ci
python -m pip install ".[integration]"

# Run this module from a tool environment containing cyclonedx-bom==7.3.1.
python -m cyclonedx_py environment /path/to/release-env/bin/python \
  --pyproject pyproject.toml --output-reproducible \
  --output-format JSON --output-file python-sbom.cdx.json --validate

npm exec --yes --package=@cyclonedx/cyclonedx-npm@6.0.1 -- \
  cyclonedx-npm --package-lock-only --omit dev --output-reproducible \
  --output-format JSON --output-file node-sbom.cdx.json
```

Review and archive both SBOMs with the distributed artifact. These generators
are release tooling, not application dependencies.

No LiteLLM Enterprise or OpenCode Enterprise source is copied into this
repository. The official LiteLLM 1.99.0 container is a mixed-license upstream
distribution that includes proprietary modules even though this project uses
only its MIT custom-auth/common-check extension path and requires no enterprise
key. Organizations that prohibit proprietary bits in runtime images should
obtain legal review and build/test an OSS-only image from the MIT-licensed
source outside LiteLLM's `enterprise/` directory.
