#!/usr/bin/env bash
# Build the portal image, push it to IBM Container Registry and create/update
# the IBM Code Engine app. Same behaviour as deploy-to-code-engine.ps1.
# Usage: ./deploy-to-code-engine.sh [env-file] [tag]
set -euo pipefail
cd "$(dirname "$0")"
ENV_FILE="${1:-.env}"; TAG="${2:-$(date +%Y%m%d-%H%M%S)}"
[ -f "$ENV_FILE" ] || { echo "Env file not found: $ENV_FILE (copy .env.example to .env)"; exit 1; }
set -a; . "$ENV_FILE"; set +a

: "${IBMCLOUD_API_KEY:?missing}" "${ICR_NAMESPACE:?missing}" "${CE_RESOURCE_GROUP:?missing}" "${CE_PROJECT:?missing}"
ICR_REGISTRY="${ICR_REGISTRY:-us.icr.io}"; IMAGE_NAME="${IMAGE_NAME:-wxo-auth-token-server}"
CE_REGION="${CE_REGION:-eu-de}"; CE_APP="${CE_APP:-wxo-auth-token-server}"; CE_REGISTRY_SECRET="${CE_REGISTRY_SECRET:-icr-secret}"
IMAGE="$ICR_REGISTRY/$ICR_NAMESPACE/$IMAGE_NAME:$TAG"

for f in Dockerfile main.py requirements.txt wxo-client-private.pem wxo-ibm-public.pem; do
  [ -f "$f" ] || { echo "Missing file: $f"; exit 1; }
done

# Application variables forwarded to Code Engine (empty values are skipped)
ENV_ARGS=()
for k in TENANT_ID CLIENT_ID CLIENT_SECRET SSO_SCOPE REDIRECT_URI SESSION_SECRET \
         WXO_AGENT_ID WXO_ORCHESTRATION_ID WXO_AGENT_ENV_ID WXO_HOST_URL WXO_CRN; do
  [ -n "${!k:-}" ] && ENV_ARGS+=(--env "$k=${!k}")
done

CTR=$(command -v podman || command -v docker) || { echo "Install docker or podman"; exit 1; }
echo "==> build + push $IMAGE"
echo "$IBMCLOUD_API_KEY" | "$CTR" login "$ICR_REGISTRY" -u iamapikey --password-stdin
"$CTR" build --platform linux/amd64 -t "$IMAGE" .
"$CTR" push "$IMAGE"

echo "==> Code Engine $CE_PROJECT ($CE_REGION)"
export IBMCLOUD_VERSION_CHECK=false
ibmcloud login --apikey "$IBMCLOUD_API_KEY" -r "$CE_REGION" -g "$CE_RESOURCE_GROUP" -q
ibmcloud ce project select -n "$CE_PROJECT"
ibmcloud ce registry delete -n "$CE_REGISTRY_SECRET" -f >/dev/null 2>&1 || true
ibmcloud ce registry create -n "$CE_REGISTRY_SECRET" -s "$ICR_REGISTRY" -u iamapikey -p "$IBMCLOUD_API_KEY"

if ibmcloud ce app get -n "$CE_APP" >/dev/null 2>&1; then
  ibmcloud ce app update -n "$CE_APP" --image "$IMAGE" --registry-secret "$CE_REGISTRY_SECRET" "${ENV_ARGS[@]}" --wait --wait-timeout 600
else
  ibmcloud ce app create -n "$CE_APP" --image "$IMAGE" --registry-secret "$CE_REGISTRY_SECRET" --port 8080 --min-scale 1 "${ENV_ARGS[@]}" --wait --wait-timeout 600
fi

URL=$(ibmcloud ce app get -n "$CE_APP" -o url | head -1)
echo; echo "OK - deployed $IMAGE"; echo "URL: $URL"
[ "${REDIRECT_URI:-}" = "$URL/auth/callback" ] || \
  echo "WARNING: set REDIRECT_URI=$URL/auth/callback in $ENV_FILE (and in the Entra App Registration), then run again."
