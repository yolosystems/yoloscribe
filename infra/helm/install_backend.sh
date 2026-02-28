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

if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  echo "Error: ANTHROPIC_API_KEY is not set in environment or .env"
  exit 1
fi

if [[ -z "${SUPABASE_JWT_SECRET:-}" ]]; then
  echo "Error: SUPABASE_JWT_SECRET is not set in environment or .env"
  exit 1
fi

if [[ -z "${GHCR_PAT:-}" ]]; then
  echo "Error: GHCR_PAT is not set"
  exit 1
fi

if [[ -z "${GITHUB_USER:-}" ]]; then
  echo "Error: GITHUB_USER is not set"
  exit 1
fi

helm upgrade --install agentscribe-backend \
  "$SCRIPT_DIR/agentscribe-backend" \
  --namespace yolo \
  --create-namespace \
  --set anthropicApiKey="$ANTHROPIC_API_KEY" \
  --set supabaseJwtSecret="$SUPABASE_JWT_SECRET" \
  --set ghcr.pat="$GHCR_PAT" \
  --set ghcr.username="$GITHUB_USER" \
  "$@"
