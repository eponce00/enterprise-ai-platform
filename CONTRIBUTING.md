# Contributing

Issues and focused pull requests are welcome. Please discuss large architectural
changes before implementation and preserve the project's core boundaries:

- clients never receive inference-provider credentials;
- LiteLLM remains the stable OpenAI-compatible gateway;
- identity and policy decisions fail closed;
- OpenCode, LiteLLM, and providers remain replaceable;
- documented OSS extension points are preferred over upstream forks.

## Development setup

Install Python 3.10+, Node.js 22.22.2+, and Docker with Compose. Then run:

```sh
python -m pip install -e ".[dev,integration]"
npm ci
```

Before submitting a pull request:

```sh
python -m ruff format --check .
python -m ruff check .
python -m mypy
python -m pytest -m "not integration and not real_provider"
npm run lint
npm run typecheck
npm test
npm run build
```

Do not commit credentials, populated `.env` files, generated live catalogs, or
prompt/completion data. Real-provider tests are opt-in and must use an approved
test environment.

Security reports belong in a private advisory as described in
[`SECURITY.md`](SECURITY.md), not in a public issue.
