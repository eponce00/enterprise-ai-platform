# Approved model catalog

Run:

```sh
python -m catalog.sync --policy catalog/catalog-policy.yaml
```

With `OPENROUTER_API_KEY`, synchronization uses `/api/v1/models/user`, whose
result reflects the account's preferences and guardrails. Without a key it uses
the public `/models?zdr=true` view. The command normalizes only operational
fields, writes atomically, and falls back to a fresh existing artifact during a
short upstream outage. Each artifact is bound to a fingerprint of the complete
catalog policy, so a policy change immediately invalidates the old fallback
rather than extending a revoked model, price ceiling, or privacy decision. It
fails closed when there is no acceptable cache.

`supported_parameters` is catalog-level evidence, not a promise that every
underlying endpoint supports a feature. Requests with tools additionally set
OpenRouter `require_parameters: true` in the server-side policy hook.

The live catalog is operational provider data and is intentionally not checked
into source control; consult counsel before redistributing a snapshot.

## Gateway consumption

The Compose stack mounts `catalog/generated/` read-only at the same path in the
gateway container. At every gateway start, `gateway.start` validates
`approved-models.json` and renders one explicit LiteLLM route named
`openrouter/<id>` for each approved model. It converts the artifact's prompt and
completion prices from USD per million tokens to LiteLLM's USD-per-token
`model_info` fields, so newly discovered models remain cost-accountable before
LiteLLM's built-in price table learns about them. Those explicit names are then
returned by the authenticated gateway `/v1/models` endpoint already consumed by
the OpenCode plugin; no client-side artifact access is required.

Catalog pricing is a startup snapshot. Refresh and roll promptly after upstream
price changes, and reconcile LiteLLM spend against OpenRouter billing rather than
treating the snapshot as an invoice.

The artifact must have schema version 1, a fingerprint matching the catalog
policy baked into the gateway image, a timezone-qualified `generated_at`, a
non-empty model list, unique raw OpenRouter IDs, and finite non-negative prompt
and completion prices. `APPROVED_CATALOG_MAX_AGE_SECONDS` defaults to 86400 and
should be no greater than the sync policy's `max_stale_seconds`.

The gateway never edits the mounted artifact. Missing, malformed, explicitly
stale, or expired artifacts emit a startup warning and produce no explicit
dynamic routes; the static aliases still start, while raw model invocation
fails closed. A malformed static LiteLLM config remains a fatal startup error.
Restart or roll the gateway after atomically publishing a new artifact.
