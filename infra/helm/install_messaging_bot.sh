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

VALUES_FILE="$SCRIPT_DIR/messaging-bot.${STAGE}.${REGION}.values.yaml"
if [[ ! -f "$VALUES_FILE" ]]; then
  echo "Error: values file not found: $VALUES_FILE"
  echo "Create it by copying messaging-bot.example.values.yaml and filling in your values."
  exit 1
fi

if [[ -z "${MESSAGING_AES_KEY:-}" ]]; then
  echo "Error: MESSAGING_AES_KEY is not set in environment or .env"
  echo "Generate one with: node -e \"console.log(require('crypto').randomBytes(32).toString('base64'))\""
  exit 1
fi

if [[ -z "${SUPABASE_SERVICE_ROLE_KEY:-}" ]]; then
  echo "Error: SUPABASE_SERVICE_ROLE_KEY is not set in environment or .env"
  exit 1
fi

if [[ -z "${GHCR_PAT:-}" ]]; then
  echo "Error: GHCR_PAT is not set in environment or .env"
  exit 1
fi

# Discord token is optional — only required when discord is in ENABLED_ADAPTERS
DISCORD_SET_ARG=""
if [[ -n "${DISCORD_BOT_TOKEN:-}" ]]; then
  DISCORD_SET_ARG="--set discordBotToken=${DISCORD_BOT_TOKEN}"
fi

helm upgrade --install yoloscribe-messaging-bot \
  "$SCRIPT_DIR/yoloscribe-messaging-bot" \
  --namespace yolo \
  --create-namespace \
  --values "$VALUES_FILE" \
  --set messagingAesKey="$MESSAGING_AES_KEY" \
  --set supabaseServiceRoleKey="$SUPABASE_SERVICE_ROLE_KEY" \
  --set ghcr.pat="$GHCR_PAT" \
  ${DISCORD_SET_ARG} \
  "$@"
