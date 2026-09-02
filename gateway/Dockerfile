# Hardened multi-architecture image for the tested LiteLLM 1.99.0 release.
FROM ghcr.io/berriai/litellm-non_root:v1.99.1@sha256:47ade0259462ec54b18bcc44ff52bba4cee878c0267f8b893070bb79b4dcfbee

COPY gateway /app/gateway
COPY catalog/__init__.py /app/catalog/__init__.py
COPY catalog/sync /app/catalog/sync
COPY catalog/catalog-policy.yaml /app/catalog-policy.yaml
COPY catalog/policy.yaml /app/policy.yaml
ENV PYTHONPATH=/app \
    LITELLM_BASE_CONFIG=/app/gateway/litellm/config.yaml \
    LITELLM_RUNTIME_CONFIG=/tmp/enterprise-ai-litellm.yaml \
    APPROVED_CATALOG_PATH=/app/catalog/generated/approved-models.json \
    CATALOG_POLICY_FILE=/app/catalog-policy.yaml \
    APPROVED_CATALOG_MAX_AGE_SECONDS=86400 \
    LITELLM_UPSTREAM_ENTRYPOINT=/app/docker/prod_entrypoint.sh

# Render explicit approved-model routes, then delegate to the pinned upstream
# entrypoint so its launch behavior remains intact.
ENTRYPOINT ["python", "-m", "gateway.start"]
