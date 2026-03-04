#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../../.env"

# Load root .env if present, without permanently polluting the environment
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

if [[ -z "${STAGE:-}" ]]; then
  echo "Error: STAGE is not set (e.g. dev, staging, prod)"
  exit 1
fi

if [[ -z "${REGION:-}" ]]; then
  echo "Error: REGION is not set (e.g. us-west-2)"
  exit 1
fi

VALUES_FILE="$SCRIPT_DIR/indexer.${STAGE}.${REGION}.values.yaml"
if [[ ! -f "$VALUES_FILE" ]]; then
  echo "Error: values file not found: $VALUES_FILE"
  exit 1
fi

if [[ -z "${GHCR_PAT:-}" ]]; then
  echo "Error: GHCR_PAT is not set in environment or .env"
  exit 1
fi

if [[ -z "${SUPABASE_SERVICE_ROLE_KEY:-}" ]]; then
  echo "Error: SUPABASE_SERVICE_ROLE_KEY is not set in environment or .env"
  exit 1
fi

helm upgrade --install agentscribe-indexer \
  "$SCRIPT_DIR/agentscribe-indexer" \
  --namespace yolo \
  --create-namespace \
  --values "$VALUES_FILE" \
  --set ghcr.pat="$GHCR_PAT" \
  --set supabaseServiceRoleKey="$SUPABASE_SERVICE_ROLE_KEY" \
  "$@"
