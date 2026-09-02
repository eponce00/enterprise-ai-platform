# LiteLLM configuration

`config.yaml` is the immutable base containing logical aliases. At container
startup, `gateway.start` reads
the read-only approved catalog and writes `/tmp/enterprise-ai-litellm.yaml` with
an explicit `openrouter/<id>` route for every fresh approved model. The wrapper
then delegates to the pinned upstream LiteLLM entrypoint with that runtime file.

Every dynamic route contains catalog-derived `input_cost_per_token` and
`output_cost_per_token` under `model_info`, following LiteLLM's documented custom
pricing format. Raw model routes exist only when present in that validated
artifact, so catalog policy cannot be bypassed by guessing an OpenRouter ID;
`gateway/auth` independently enforces the caller's team model policy.

Missing, stale, or invalid catalog data never overwrites the base file and does
not prevent the alias-only local stack from starting. Startup logs the fallback,
and only static aliases appear in the rendered config.

Both `enable_post_custom_auth_checks` and `custom_auth_run_common_checks` are
security settings, not optimizations. They make LiteLLM apply model, expiry,
rate, user/team, and database-backed budget checks after custom auth.

Alias model, base URL, and API key are independent environment variables. To
move `general-fast` from OpenRouter to a direct OpenAI-compatible provider,
change those server-side variables to the direct provider's adapter, base URL,
and secret. Clients continue sending the same model name, and no separate
privacy-route allowlist can drift from the deployment.

All deployments of one public model and all members of a fallback chain must use
the same backend class. Mixed OpenRouter/direct routing fails closed so failover
cannot weaken or misapply provider-specific privacy fields.
