#!/usr/bin/env sh
set -eu

: "${OIDC_TOKEN_URL:?set OIDC_TOKEN_URL}"
: "${OIDC_CLIENT_ID:?set OIDC_CLIENT_ID}"
: "${OIDC_CLIENT_SECRET:?set OIDC_CLIENT_SECRET}"
: "${GATEWAY_URL:?set GATEWAY_URL}"

if [ -n "${OIDC_SCOPE:-}" ]; then
  TOKEN="$(curl --fail --silent --show-error \
    --data-urlencode grant_type=client_credentials \
    --data-urlencode client_id="$OIDC_CLIENT_ID" \
    --data-urlencode client_secret="$OIDC_CLIENT_SECRET" \
    --data-urlencode scope="$OIDC_SCOPE" \
    "$OIDC_TOKEN_URL" | jq -er .access_token)"
else
  TOKEN="$(curl --fail --silent --show-error \
    --data-urlencode grant_type=client_credentials \
    --data-urlencode client_id="$OIDC_CLIENT_ID" \
    --data-urlencode client_secret="$OIDC_CLIENT_SECRET" \
    "$OIDC_TOKEN_URL" | jq -er .access_token)"
fi

curl --fail --silent --show-error \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Enterprise-AI-Client: curl-example" \
  -d '{"model":"general-fast","messages":[{"role":"user","content":"Reply with: ok"}]}' \
  "$GATEWAY_URL/chat/completions"
